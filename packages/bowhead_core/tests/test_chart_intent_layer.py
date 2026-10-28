"""意图层：hint 真正参与选型（chart-config-ux 批1 ①）.

起因（116 live 窗口 1704 次调用实测）：`hint` 是 `add_chart` 的必填参数，
但此前 `infer_chart_spec` 只读 bridge/flow/share/correlation 四个取值——**92.3%
的调用（rank 41.9% / trend 26.6% / compare 14.2%）对最终图种零影响**。模型说了
没人听，只好绕道 `force_kind` 再说一遍（22.4% 的图是这么来的）。

判定树按形状选型的设计不变；意图层只在**形状允许的前提下**表达「用户想看什么」。
"""

from __future__ import annotations

from bowhead_core.chart import ChartColumn, infer_chart_spec


def _cols(*defs: tuple[str, str]) -> list[ChartColumn]:
    return [ChartColumn(key=k, header=k, type=t) for k, t in defs]  # type: ignore[arg-type]


def test_rank_hint_gives_hbar_even_with_short_labels():
    """排名 → 横条，不看类目名长短.

    这正是形状规则 11 结构上给不出的那一档：live 窗口模型 force 的 85 张 hbar 里，
    70 张类目名 ≤6 字，而规则 11 要求 `max_name_len > 6 且 n >= 10`，永远不成立。
    """
    rows = [{"省": f"P{i}", "GMV": 100 - i} for i in range(10)]
    spec = infer_chart_spec(rows, _cols(("省", "geo"), ("GMV", "numeric")), hint="rank", title="各省 GMV 排名")
    assert spec.kind == "hbar"


def test_rank_hint_too_few_rows_falls_back_to_shape():
    """类目太少时不给横条（4 根横条在报告里空得发慌）——交回形状树."""
    rows = [{"省": f"P{i}", "GMV": 100 - i} for i in range(4)]
    spec = infer_chart_spec(rows, _cols(("省", "geo"), ("GMV", "numeric")), hint="rank", title="t")
    assert spec.kind == "bar"


def test_rank_hint_preempts_geo_branch():
    """意图层必须排在 geo 分支之前.

    geo 分支此前无条件吃掉所有含地理列的图（live 窗口 12.7%），「各省 GMV 排名」
    于是永远是竖柱。把意图层挪到规则 3 之后，这条就红。
    """
    rows = [{"省": f"省{i}", "GMV": 100 - i} for i in range(12)]
    spec = infer_chart_spec(rows, _cols(("省", "geo"), ("GMV", "numeric")), hint="rank", title="t")
    assert spec.kind == "hbar"


def test_rank_hint_yields_to_dual_axis_when_magnitudes_differ():
    """多指标且量级差大时，排名**不**抢横条——横条共用一根值轴会把小的那个压没.

    真机抽检逮到的回归：「品牌营业额 Top 20 及占比」两列是 元（百万级）与 占比%
    （0~100），一律改横条后占比被压成贴着零的一条线。摘掉 `shares_one_axis`
    这条就红。
    """
    rows = [{"品牌": f"B{i}", "营业额": 5_000_000 + i, "占比%": 5 + i / 10} for i in range(20)]
    spec = infer_chart_spec(
        rows,
        _cols(("品牌", "categorical"), ("营业额", "numeric"), ("占比%", "numeric")),
        hint="rank",
        title="品牌营业额 Top 20 及占比",
    )
    assert spec.kind == "dual_axis"


def test_rank_hint_keeps_hbar_when_metrics_share_a_scale():
    """量级相当的多指标仍走横条——闸卡的是量级差，不是「有几列」."""
    rows = [{"门店": f"S{i}", "今年": 100 + i, "去年": 90 + i} for i in range(12)]
    spec = infer_chart_spec(
        rows,
        _cols(("门店", "categorical"), ("今年", "numeric"), ("去年", "numeric")),
        hint="rank",
        title="门店营业额排名",
    )
    assert spec.kind == "hbar"


def test_trend_hint_without_time_axis_does_not_fake_a_line():
    """没有 date 列就不假装时间轴——在类目轴上画折线是在暗示不存在的连续性."""
    rows = [{"品牌": f"B{i}", "销量": i} for i in range(10)]
    spec = infer_chart_spec(rows, _cols(("品牌", "categorical"), ("销量", "numeric")), hint="trend", title="t")
    assert spec.kind != "line"


def test_compare_hint_defers_to_shape_tree_when_magnitudes_match():
    """量级相当时对比意图**不发言**——形状树本来就选得对，而且它还会按标签长度
    在 bar / hbar 之间挑（规则 11）。抢过来会把那条判断丢掉：真机抽检逮到 3 张
    长品牌名的对比图从 hbar 退回 bar。

    把 compare 分支的 `n_axes == 2` 守卫摘掉，本例就红。
    """
    rows = [{"品牌名称超过六个字的那种": f"某某某某某某某{i}", "今年": 100 + i, "去年": 90 + i} for i in range(12)]
    spec = infer_chart_spec(
        rows,
        _cols(("品牌名称超过六个字的那种", "categorical"), ("今年", "numeric"), ("去年", "numeric")),
        hint="compare",
        title="t",
    )
    assert spec.kind == "hbar", "长类目名的对比图应保持横条（规则 11），意图层不该抢"


def test_compare_hint_dual_axis_when_magnitudes_differ():
    rows = [{"省": f"P{i}", "营业额": 10_000_000 + i, "利润率": 0.1 + i / 100} for i in range(8)]
    spec = infer_chart_spec(
        rows,
        _cols(("省", "geo"), ("营业额", "numeric"), ("利润率", "numeric")),
        hint="compare",
        title="t",
    )
    assert spec.kind == "dual_axis"


# ── Positive controls: removing the guard must fail both tests ──


def test_intent_never_draws_more_rows_than_the_cap():
    """行数上限对每个意图都成立——现在由**截断**保证，不再是「退成表」。

    ⚠ 本例原来断言 `kind == "table"`：那是批3 ⑤ 之前的形态（`n>30 → table`）。
    上限本身没变，变的是超限时怎么办——所以断言从「退表」改成「画出来的行数不超上限，
    且总量口径没被改写」。把 `infer_chart_spec` 里那段截断摘掉，本例就红。

    维度仍用 categorical 不用 geo：geo 分支自己不看行数（既有缺口），
    混进来会让本例分不清是谁在守。
    """
    cases = (
        ("rank", (("门店", "categorical"), ("GMV", "numeric")), lambda i: {"门店": f"S{i}", "GMV": i}),
        ("compare", (("门店", "categorical"), ("今年", "numeric"), ("去年", "numeric")), lambda i: {"门店": f"S{i}", "今年": i, "去年": i + 1}),
        ("composition", (("门店", "categorical"), ("A", "numeric"), ("B", "numeric")), lambda i: {"门店": f"S{i}", "A": i, "B": i + 1}),
    )
    for hint, cols_def, build in cases:
        rows = [build(i) for i in range(60)]  # 60 > _MAX_BAR_ROWS
        spec = infer_chart_spec(rows, _cols(*cols_def), hint=hint, title="t")
        # ⚠ Two caps by chart kind since 2026-09-02: vertically expanding, internally scrolling hbar / table use
        #   the inline cap; categories on the horizontal axis (vertical bars/lines) keep _MAX_BAR_ROWS. Formerly fixed at 30,
        #   this failed the rank case when hbar expanded; the cap was split, not bypassed.
        cap = 50 if spec.kind in ("hbar", "table") else 30
        assert len(spec.rows or []) <= cap, f"{hint} 画了 {len(spec.rows or [])} 行（{spec.kind}）"
        assert spec.top_n is not None, f"{hint} 截断了却没标记"
        assert spec.row_count == 60, f"{hint} 把总量改写成了展示行数"


def test_intent_never_invents_an_unrenderable_chart():
    """形状守卫：数据不支持时意图必须交回形状树，而不是硬出图."""
    rows = [{"品牌": f"B{i}", "销量": i} for i in range(10)]
    cols = _cols(("品牌", "categorical"), ("销量", "numeric"))
    # correlation needs two numeric columns; only one here → no scatter.
    assert infer_chart_spec(rows, cols, hint="correlation", title="t").kind != "scatter"
    # composition needs multiple components; one dimension and metric → no stacking.
    assert infer_chart_spec(rows, cols, hint="composition", title="t").kind != "stacked_bar"


def test_geo_counts_as_a_dimension_in_every_intent_branch():
    """★ 意图层只有**一种**维度口径：`省/市` 也是维度。

    116 dogfood 逮到（2026-09-01）：`各省营业额占比` 出的是柱状不是饼图。根因是
    规则 5b/5c/6/7 搬进意图层时「判据一字未改」——**包括它们用 `cat_cols` 而不是
    `dims`**，于是同一个函数里两种口径：我新写的分支认 geo，搬来的四条不认。
    语料复算：42 次 share 调用里 13 张形状满足却被挡（能进的只有 5 张）。

    把任一条改回 `cat_cols`，本例对应那项就红。
    """
    # share → pie (observed rejected shape: shares for five provinces)
    rows = [{"省": f"P{i}", "占比": 20 - i} for i in range(5)]
    assert infer_chart_spec(rows, _cols(("省", "geo"), ("占比", "numeric")), hint="share", title="各省占比").kind == "pie"

    # bridge → waterfall
    rows = [{"省": f"P{i}", "增量": i - 2} for i in range(8)]
    assert infer_chart_spec(rows, _cols(("省", "geo"), ("增量", "numeric")), hint="bridge", title="t").kind == "waterfall"

    # flow → sankey (province → city is a typical Sankey shape)
    rows = [{"省": f"P{i % 3}", "市": f"C{i}", "流量": i + 1} for i in range(10)]
    assert infer_chart_spec(rows, _cols(("省", "geo"), ("市", "geo"), ("流量", "numeric")), hint="flow", title="t").kind == "sankey"

    # composition → stacked_bar
    rows = [{"省": f"P{i % 3}", "市": f"C{i}", "额": i + 1} for i in range(9)]
    assert infer_chart_spec(rows, _cols(("省", "geo"), ("市", "geo"), ("额", "numeric")), hint="composition", title="t").kind == "stacked_bar"


def test_migrated_hint_rules_still_hold_on_categorical_dims():
    """5b/5c/6/7 搬家时的原有行为（类目维度那半）不许丢。

    ⚠ 搬进来时判据确实「一字未改」，但那**连它们用 `cat_cols` 而不是 `dims` 也一起
    继承了**——116 dogfood 逮到后已统一成 `dims`（见上一条用例）。
    本例守的是另一半：口径放宽之后，原本就能进的类目维度形状**仍然照常进**。
    """
    # bridge → waterfall
    rows = [{"科目": f"S{i}", "增量": i - 3} for i in range(8)]
    assert infer_chart_spec(rows, _cols(("科目", "categorical"), ("增量", "numeric")), hint="bridge", title="t").kind == "waterfall"
    # share → pie (3..8 rows)
    rows = [{"渠道": f"C{i}", "占比": i} for i in range(5)]
    assert infer_chart_spec(rows, _cols(("渠道", "categorical"), ("占比", "numeric")), hint="share", title="t").kind == "pie"
    # correlation → scatter
    rows = [{"客流": i * 10, "营业额": i * 100} for i in range(12)]
    assert infer_chart_spec(rows, _cols(("客流", "numeric"), ("营业额", "numeric")), hint="correlation", title="t").kind == "scatter"
    # flow → sankey only for a real flow. The original `A{i} -> B{i}` pairs share no node: ten independent bands,
    # which `_not_a_flow_reason` turns into a table (readability audit item 1).
    rows = [{"来源": f"A{i}", "去向": f"B{i}", "流量": i} for i in range(10)]
    disjoint = infer_chart_spec(rows, _cols(("来源", "categorical"), ("去向", "categorical"), ("流量", "numeric")), hint="flow", title="t")
    assert disjoint.kind == "table", disjoint._infer_reason
    # Sources fanning out to several targets form a flow and stay a Sankey.
    rows = [{"来源": f"A{i % 3}", "去向": f"B{i}", "流量": i + 1} for i in range(10)]
    assert (
        infer_chart_spec(
            rows,
            _cols(("来源", "categorical"), ("去向", "categorical"), ("流量", "numeric")),
            hint="flow",
            title="t",
        ).kind
        == "sankey"
    )
