"""infer_chart_spec —— 基于 rows shape + hint 推断 ChartSpec.

设计决策（见 docs/native-charts-prd.md §3 批判性阅读）:
    - 按 **data shape** 选图表，不按 NL 句式/关键词
    - hint 是粗粒度偏好（trend / rank / share / ...），非强制
    - 所有分支必有**表格兜底**，没 render 不了的情况
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from typing import Any

from bowhead_core.chart.types import (
    VALID_HINTS,
    ChartColumn,
    ChartKind,
    ChartSpec,
    spec_to_dict,
)

# Cap line-family charts by **time-point count**, not row count.
#
# ⚠ The old >200-row table fallback ignored that long-format rows = time points × series:
# 20 brands × 12 months = 240 rows, only 12 time points, incorrectly treated as too long and converted to a table.
# On 116, four of 46 time-axis charts that became tables hit this rule:
# `各省月度刷卡金额走势`: 708 rows (a dozen-plus months × 50 provinces); `每日营业额趋势`: 365 rows (daily for a year).
# Both are legitimate trend charts that users actually requested.
#
# Use 3000 time points. Basis (2026-09-21 research/testing; see
# `docs/sprints/chart-timeseries-series-2026-09-21/spike.md`）：
#   · Power BI: 3500 data points per visual; ThoughtSpot: 20,000 by default with horizontal scrolling;
#     Metabase: 2000 rows; Tableau: 1000–2000 marks per chart recommended. 3000 sits within this range.
#   · Chart.js 4.5.1 rendered 3500 points × six series on an 820×360 canvas in just 16.5ms
#     (597ms total animation, 168ms longest frame), so **rendering cost is not the constraint**.
#   · Screenshot inspection: trends/seasonality remain readable for one series with 3500 points (~9.5 daily years);
#     the previous claim that everything blurs together does not hold for a single series.
#
# ⚠ Honest qualification: the observed 116 maximum was only 244 time points, with none >365; **benefit cannot be measured today**.
# This buys headroom once retrieval removes its default first-29-days window when no time range is specified.
#
# Above the cap, **do not immediately fall back to a table**; try LTTB downsampling (`_downsample_time_axis`) first. No BI product
# replaces lines with tables on overflow. Series counts have two separate gates: >15 keeps the first 15 (`_MAX_TIME_SERIES`);
# >100 falls back to a table (`_MAX_DRAWN_SERIES`).
_MAX_LINE_POINTS = 3000

# Downsample only line-family charts whose horizontal axis is **continuous time**.
# Deliberately exclude bar-family charts (bar / stacked_bar / waterfall): removing bars makes
# bar widths and gaps misleading, because readers interpret adjacent bars as adjacent time points.
_DOWNSAMPLE_KINDS = frozenset({"line", "area_stacked", "dual_axis", "combo"})

# Maximum readable bars on the horizontal axis: **independent of `_MAX_LINE_POINTS`**.
#
# ⚠ That constant is checked by _undrawable_reason against _TIME_AXIS_KINDS, which also includes bar/stacked_bar/waterfall.
# Raising it from 1000 to 3000 inadvertently tripled the bar cap, although bars have **no sampling fallback**
# (sampling distorts widths/gaps): 2500 bars on 820px are each 0.33px wide.
# Previously these became tables; review testing found 1200/2500 rows changed from table to chart for all three force_kind values.
# All evidence for 3000 came from **lines**, not bars; keep the bar-family cap at 1000.
_MAX_BAR_SLOTS = 1000

# **Lower bound** for second-pass downsampling to fit the byte budget; below it, use the existing table fallback.
# Use `_MAX_LINE_POINTS / 10`: sparser output becomes a different chart, not the same curve with fewer points.
# Deep downsampling also invalidates the caption claim that each series retains extremes and endpoints:
# point selection sums absolute values across series, so large-scale series can hide smaller spikes, as confirmed in review.
# The table is complete; prefer giving that.
_MIN_DOWNSAMPLE_POINTS = _MAX_LINE_POINTS // 10

# Chart kinds whose horizontal axis is time. **Exclude horizontal bars**: their horizontal axis is value and time is a category label;
# top-N by value remains a meaningful ranking (force_kind="hbar" with a date column).
# Scatter/pie/heatmap/Sankey likewise have their own axis semantics.
_TIME_AXIS_KINDS = frozenset({"line", "area_stacked", "bar", "stacked_bar", "dual_axis", "combo", "waterfall"})
_MAX_BAR_ROWS = 30
_MAX_PIE_ROWS = 8
_MIN_PIE_ROWS = 3
# Maximum series with **different units** in one chart, matching the base frontend palette slots
# (`lib/chart-theme.ts` PALETTE_LIGHT / PALETTE_DARK, six colors each).
# test_chart_series_capacity.py compares against that palette exactly, preventing independent drift.
#
# ⚠ Formerly `_MAX_NUMERIC_COLS = 3`, meaning too many columns to draw leads to table fallback. Production evidence showed
# this was too coarse: it counted columns without asking whether they **share a scale**. `2025年月度核心指标趋势` had
# 12 rows × four monetary measures, suitable for four lines, but fell back merely because four columns were counted.
# Drawability now depends on unit-family count (`_axis_groups`), series count, and mark count.
_MAX_SERIES = 6

# A separate cap for **one unit family** (one measure split into N series, such as `各品类月度营业额`).
#
# Why separate limits (116 dogfood 1.2.28, sprint document §25): same/different families previously shared
# _MAX_SERIES because exceeding palette slots was said to repeat colors. seriesColor() already extends
# the palette by interpolating toward surface/ink on wraparound, so **that rationale no longer holds**. Yet the gate still blocked
# charts and the model followed that rationale, telling the user: `因10条线超出原生图表6色调色板`
# `容量，add_chart 退化为数据表，故用 add_image 挂 10 线折线图`, handing the work to matplotlib.
#
# Use 12, **two palette cycles** (six original colors + six faded toward the background). This follows the seriesColor
# mechanism: the second cycle remains visibly distinguishable lighter shades; subsequent cycles start to blur hue differences.
# Corpus evidence: eight same-family charts had >6 columns, with **at most ten** (`各品类月度趋势` / `各省逐月趋势` /
# `Monthly 营业额 by Category`). Twelve covers all with headroom. All >10-column charts had **different families**
# (`各省四指标并排明细`, `近三年月度趋势`) and still fall back under _MAX_SERIES, appropriately as tables.
#
# Category-axis density is bounded by _display_row_cap's mark budget, narrowing rows according to series count
# (12 series -> eight rows). Time axes are already untruncated; ten categories × 12 months is a normal line chart.
_MAX_SERIES_SAME_FAMILY = 12

# Category-axis mark budget (series × rows): four series × 30 rows = 120 unreadable bars.
# Use _MAX_BAR_ROWS * 3: 30 bars for one series is accepted density; triple that is the practical grouped-bar limit.
# Time axes are unaffected because line families are not truncated; six lines × 12 months is fine.
_MARKS_BUDGET = 30 * 3
# Minimum rows after narrowing: even a tight budget must leave the trend readable.
_MIN_TRUNCATED_ROWS = 8
_LONG_LABEL_CHARS = 6  # Category names longer than six characters favor horizontal bars.
# Minimum category count for ranking horizontal bars: few categories look better vertically (four horizontal bars look empty in reports).
_MIN_RANK_HBAR_ROWS = 5

# Large row counts do not impair readability for these kinds, so do not truncate them:
#   table: its own pagination/internal scrolling (footer `显示 1-30 / 共 38` + Expand all).
#   hbar: categories are vertical; container height grows with them and overflows scroll (.v2-chart-canvas-wrap.is-tall).
#   scatter: **no category axis at all**. _MAX_BAR_ROWS measures readable bars and has no meaning here;
#              worse, top-N by value **cuts off an entire coordinate-axis range**.
#              116 dogfood (`s_71e3a520634e`, 1.2.23): a 39-store
#              `门店面积 vs 营业额` chart was cut to 30 points despite its subtitle saying `39 stores · Pearson`
#              `r = 0.54` (calculated from all 39), and prose cited `五棵松 (1,135 m²)`
#              and `花园北路 (1,655 m²)` as evidence, but **neither point appeared** and the x-axis began at 6K.
#              The chart contradicted its own title and prose.
# Vertical bars / line families are excluded here: horizontal categories crowd with many rows and still need truncation.
_ROWS_SCROLLABLE_KINDS = frozenset({"table", "hbar", "scatter"})

# Every plotted row must be present in the chart spec: main charts, document
# exports, and cards do not fetch omitted rows. Tables have separate pagination.
# The byte-size gate applies before the row-count fallback. Any overflow must set
# top_n so truncation is visible. Ranking horizontal bars use the inline limit
# because thousands of bars exceed practical canvas heights; both restoration and
# the date-column branch preserve that exception.
_CHART_ROWS_MAX = 50_000

# Maximum **drawable** chart series, counted from normalized frozen export presentation; see _undrawable_reason.
#
# The former 50-row spec cap incidentally limited series after long-format pivoting to 50. Without it,
# daily trends for 300 SKUs yield 300 unreadable lines; frozen export presentation then exceeds
# MAX_FROZEN_CHART_PRESENTATION_BYTES (64KB, observed from ~170 series), **failing the entire export**.
# Use 100: frozen presentation measured 37KB for 100 series, leaving sufficient margin below 64KB.
# Automatic charts can also hit it: sparse long-format data (two months, dozens of distinct SKUs per month, ≤200 rows) can pivot to hundreds of series.
# Previously the 50-row cap silently drew half; now table fallback is appropriate, but **the automatic path is not wholly unchanged**.
#
# ⚠ This answers drawability, **not readability**; _MAX_TIME_SERIES governs the latter.
_MAX_DRAWN_SERIES = 100

# Maximum **readable** series on a time axis.
#
# Separate from _MAX_DRAWN_SERIES, which asks whether frozen exports fit (100 series); this asks whether
# people can read them. Testing 59 stacked-area series in 820×360 gave six legend rows consuming ~200px,
# with 59 indistinguishable bands below and no readable province, worse than table fallback.
#
# ⚠ This gate **did not previously exist**; the >200-time-series-row fallback incidentally
# blocked all long-format multi-series charts (708 rows = 12 months × 59 provinces).
# After 4bd93dfc9 switched to **time-point count**, that incidental protection disappeared, and four actual 116 charts
# changed from tables to 36–59-layer stacked-area charts, already shipped in production with 1.3.19.
#
# Use 15, matching the existing line / area_stacked boundary (rule 4 and intent-layer uniq > 15);
# no new threshold, with ≤15-series charts byte-identical. Wide-format data uses _draw_series_cap
# (same-family 12 / different-family six). The difference is **intentional**: add only the missing gate,
# without tightening limits for currently drawable charts.
_MAX_TIME_SERIES = 15

# Byte cap for normalized structure (normalizedRenderShape, the main part of frozen presentation).
# Frozen presentation has two **independent** size-growth terms (measured 2026-09-19):
#   frozen bytes ≈ structure bytes + ~2KB fixed + ~0.27KB per series (resolved per-series style).
# _MAX_DRAWN_SERIES bounds series count. Stacked-bar structure records stack edges for **every category**
# (barStackEdges), growing with categories independently of series. A forced stacked bar with 7000 dates
# and just one series still exceeds 64KB of frozen presentation and fails the whole export (third-round review).
# Use 30KB: at both caps, 30 + 100×0.27 + 2 ≈ 59KB, still below 64KB.
_MAX_DRAWN_SHAPE_BYTES = 30_000

# Cap category × group cells for unpivoted stacked bars **before normalization**.
# Normalization materializes the whole category×group matrix: sparse 5000×5000 stacked bars need tens of seconds and over 400MB,
# synchronously on the Gateway event loop. Under the two gates above, drawable stacked bars have at most 100 groups
# and roughly _MAX_DRAWN_SHAPE_BYTES / 7 categories (~7 stack-edge bytes per category). Above the product of those limits,
# skip computation and fall back directly. This is a **conservative** upper bound, not exact: categories can use only ~4 bytes, so a contrived
# 4300-category × 100-group shape with only two nonzero groups per category may fit frozen presentation yet fall back; errors only favor extra tables.
# Pivoted stacked bars cannot reach this case: pivoting requires fill ≥1/3, so cells ≤3 × rows.
_MAX_STACK_CELLS = _MAX_DRAWN_SERIES * (_MAX_DRAWN_SHAPE_BYTES // 7)

# Maximum serialized rows bytes per chart. Row caps cannot bound **wide tables and long text**:
# 116 measurements: a 13-column, 10,000-row line chart was 5.34MB; a 10,000-point scatter with full store names was 1.92MB.
# Export browser input caps each chart at 4MB (export_browser_visual.MAX_BROWSER_INPUT_BYTES)
# and the whole snapshot at 10MB (export_snapshot.MAX_SNAPSHOT_PAYLOAD_BYTES), so one chart can fail the whole export.
# Use 1MB: an ordinary three-column × 10,000-row table is ~840KB and remains unaffected.
#
# ⚠ This batch briefly raised the cap to 2MB because 3500 points × six long-format series were ~1.4MB; 1MB would truncate
# and trigger time-series-to-table fallback. **Review rejected that rationale**: with downsampling, the same chart still fits 1MB,
# sampling to 2363 time points rather than 3000, visually indistinguishable.
# The measured cost of 2MB is real: the whole-snapshot 10MB gate is
# an **unconditional** raise SnapshotError("snapshot_too_large") at export_snapshot.py:343,
# without per-chart fallback (except ChartPresentationTooLargeError only catches the 64KB frozen-presentation limit).
# This halves maximum-size charts per report from ~ten to ~five; exceeding it **fails the entire PPTX/PDF export**.
# Halving whole-export headroom for a slightly denser version of the same chart is not worthwhile.
#
# On overflow, try time-axis line-family downsampling first (_downsample_time_axis drops points, not time ranges);
# other kinds keep tail truncation, but **must set top_n**.
# ⏳ Existing limitation, not introduced here: the 10MB gate lacks per-chart fallback, unlike the 64KB gate.
_CHART_BYTES_MAX = 1_000_000

# Maximum heatmap vertical-axis categories (second dimension in column order; build-config.ts buildHeatmap).
# Cell height = plot height / categories − 2; a ~400px plot with 40 categories gives ~8px cells. More leaves only borders
# and a background-colored chart; review measured 1px cells for 100 stores, producing a blank chart.
# Automatic heatmaps cannot hit this: charts without time axes are first capped at 30 rows by _display_row_cap.
_MAX_HEATMAP_Y = 40


def _col_attr(col: Any, name: str) -> Any:
    """`columns` 两种形态都要吃：`ChartColumn` 对象（后端内部）与 plain dict
    （`inline_artifacts` 里过了一趟 JSON 的 spec）。"""
    if isinstance(col, dict):
        return col.get(name)
    return getattr(col, name, None)


def indistinguishable_rows_reason(rows: list[dict[str, Any]], columns: list[Any]) -> str | None:
    """Detect multiple rows whose categorical labels are all constant.

    Purely numeric scatter data remains valid. This is a narrow validation guard
    after column projection, not a replacement for preserving distinguishing columns.
    """
    if len(rows) < 2:
        return None
    label_keys = [k for c in columns if (k := _col_attr(c, "key")) and _col_attr(c, "type") != "numeric"]
    if not label_keys:
        return None
    constants: list[str] = []
    for key in label_keys:
        seen = {str(r.get(key)) for r in rows}
        if len(seen) > 1:
            return None  # Rows are distinguishable if any column differentiates them.
        constants.append(f"{key}={next(iter(seen))!r}")
    return f"{len(rows)} 行的标签列全部取同一个值（{', '.join(constants[:3])}）"


def _draw_series_cap(rows: list[dict[str, Any]], num_cols: list[ChartColumn]) -> int:
    """这组数值列最多允许几条序列还画得出——**按量纲族分档**。

    同族 = 一个指标切成 N 份（`各品类月度营业额`），多画几条只是密一点；
    异族 = N 个不同指标并排，挤一根轴上小的会被压成贴零直线。
    见 `_MAX_SERIES_SAME_FAMILY` 的注释。

    ⚠ 退表闸和形状树**必须用同一个数**。第一版我只放宽了退表闸，形状树里规则 3/11
    仍写死 `_MAX_SERIES`：闸放行了、形状树又接不住，7 列同族的图掉到规则 12 的
    兜底表格——换汤不换药，`_infer_reason` 还从「series 太多」变成了更没信息量的
    「unmatched shape」。单测当场逮到。
    """
    if len(num_cols) < 2:
        return _MAX_SERIES_SAME_FAMILY
    return _MAX_SERIES_SAME_FAMILY if len(_axis_groups(rows, num_cols)) == 1 else _MAX_SERIES


def _display_row_cap(norm_hint: str, n_series: int = 1) -> int:
    """这个意图 + 这么多条序列下，「一张图最多画多少行还读得动」。

    两个维度：

    - **按 hint** —— 桑基自己的形状守卫就是 `n <= 60`，统一砍到 30 会丢掉一半流向。
    - **按序列数** —— 一行一根柱时 30 根是可读的，四条序列并排就是 120 根。
      多序列时按 `_MARKS_BUDGET` 收窄行数，让**总标记数**维持在同一量级。

    时间轴走不到这里（`infer_chart_spec` 里 `has_date` 早退）——折线族本来就不截断，
    6 条线 × 12 个月是完全正常的图（它的上限按**时间点数**算，见 `_MAX_LINE_POINTS`）。
    """
    cap = 60 if norm_hint == "flow" else _MAX_BAR_ROWS
    if n_series > 1:
        cap = min(cap, max(_MIN_TRUNCATED_ROWS, _MARKS_BUDGET // n_series))
    return cap


def _time_points(rows: list[dict[str, Any]], date_cols: list[ChartColumn]) -> int:
    """这份数据在时间轴上有几个**不同的时刻**。

    ⚠ 它**不等于图上画出来的 x 槽位数**：只有前端 `pivotLongSpec` 生效时（恰好 1 个数值列 +
    恰好 2 个维度 + 填充率 ≥1/3）长表才会摊成「一个时刻一个槽位」；否则一行就是一个槽位。
    所以这里只用来回答「这是不是一条真的时间轴」（至少两个时刻），
    **上限由出口按 `categoryCount` 判**（`_undrawable_reason`，与导出/渲染同一份正规化）。
    """
    if not date_cols:
        return 0
    key = date_cols[0].key
    return len({str(r.get(key)) for r in rows})


def _take_top_n(
    rows: list[dict[str, Any]],
    num_cols: list[ChartColumn],
    cap: int,
) -> list[dict[str, Any]]:
    """行数超上限时取前 `cap` 行——**按主数值列降序**，取不到数值列就保持原序。

    ⚠ 必须在 `inline_rows` 切片**之前**跑：`rows` 一旦被切成头 50 行，
    「前 50 行里的前 30」就不是真的 top-30 了（真机有 9183 行的图）。

    ⚠ 只在**必须截断时**排序——不截断的图一行不动。重排会改变 agent 给的行序，
    那是它的表达；只有在「必须丢掉一部分」这个前提下，「丢最小的那些」才是
    唯一说得过去的选法。

    尾巴**丢弃而不是折成「其余」**：会触发截断的基本都是排名类，折出来的桶通常是
    一根压扁其他所有柱子的巨柱。折叠语义留在检视层（`applyView`），那是用户主动
    选的，且比率列不求和的诚实处理已经写在那里。
    """
    if len(rows) <= cap:
        return rows
    primary = next((c for c in num_cols), None)
    if primary is None:
        return rows[:cap]

    def _key(r: dict[str, Any]) -> float:
        v = r.get(primary.key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return float("-inf")
        return float(v)

    return sorted(rows, key=_key, reverse=True)[:cap]


def _kind_from_intent(
    norm_hint: str,
    rows: list[dict[str, Any]],
    cat_cols: list[ChartColumn],
    num_cols: list[ChartColumn],
    date_cols: list[ChartColumn],
    geo_cols: list[ChartColumn],
    n: int,
) -> tuple[ChartKind, str] | None:
    """意图 → 图种。**hint 的全部判据只住这一处**；形状判定仍归下面的判定树。

    为什么要有它（116 live 窗口 1704 次调用实测）：`hint` 是 `add_chart` 的
    必填参数，但此前 `infer_chart_spec` 只在 bridge/flow/share/correlation 四处读它
    —— **92.3% 的调用（rank 41.9% / trend 26.6% / compare 14.2%）对最终图种没有任何
    影响**。模型说了没人听，只好绕道 `force_kind` 再说一遍（22.4% 的图是这么来的）。
    拆开 hbar 看得最清楚：135 张里 85 张是模型 force 的，其中 70 张类目名 ≤6 字
    —— 规则 11 要求 `>6 字且 n>=10`，**结构上永远判不出来**；而 85/135 的标题带
    「排名 / Top」。模型要的是 rank 语义，判定树看的却是标签长度。

    返回 `None` = 这个意图在当前数据形状下不成立，交回判定树按形状选。
    **意图不能凭空造出画不出的图** —— 每条分支都自带形状守卫，这是本函数的硬约束。
    """
    dims = cat_cols + geo_cols
    # How many value axes these measures need: use **the same** criterion as rule 10 (`_axis_groups`, unit families only).
    # Do not group on one path and compare max/min scales on another; that duplicates one concept with two implementations.
    n_axes = len(_axis_groups(rows, num_cols)) if len(num_cols) >= 2 else 1

    # `infer_chart_spec` now enforces the row cap through truncation (n is already ≤ cap here).
    # Each branch still has its **own** guard. This is not redundant: it is this function's independent contract,
    # preventing silent 60-bar charts if callers change or truncation is removed. The line family uses `_MAX_LINE_POINTS`.

    # Ranking -> horizontal bars. See `_MIN_RANK_HBAR_ROWS` for the lower bound.
    #
    # ⚠ Multiple measures must also pass the **unit-family gate**: horizontal-bar series share one value axis, so a different unit
    # becomes an effectively invisible line near zero. Device sampling found exactly this with `品牌营业额 Top 20`
    # plus `及占比`: the columns were currency and percentage. dual_axis originally gave the percentage the right axis; forcing horizontal bars
    # hid it (most of 26 dual_axis->hbar charts had this shape: refund amount/count/rate).
    # If two axes are needed, return to the shape tree so rule 10 selects dual_axis; readability takes priority over horizontal ranking.
    if norm_hint == "rank" and dims and 1 <= len(num_cols) <= _draw_series_cap(rows, num_cols):
        shares_one_axis = n_axes == 1
        if shares_one_axis and _MIN_RANK_HBAR_ROWS <= n <= _MAX_BAR_ROWS:
            return "hbar", f"hint=rank + {len(dims)} dim + {len(num_cols)} num + n={n} → hbar"

    # Trend -> line. A **real time axis is required**: do not invent one without a date column (a line on a category axis
    # implies nonexistent continuity); return to the decision tree.
    # A time axis needs ≥2 distinct times: 500 rows from one day are not a trend (`_constant_dim_keys` detects constants only with
    # ≥2 dimension columns, so it cannot detect this single-dimension case).
    if norm_hint == "trend" and date_cols and _time_points(rows, date_cols) >= 2:
        if cat_cols and len({r.get(cat_cols[0].key) for r in rows}) > 15:
            return "area_stacked", "hint=trend + time + cat unique>15 → area_stacked"
        return "line", f"hint=trend + time + {_time_points(rows, date_cols)} 个时刻 → line"

    # Comparison -> dual axis when two axes are needed (same `_axis_groups` criterion as rule 10).
    #
    # ⚠ Intervene only when adding **actual value**: with similar scales the shape tree already chooses correctly and also selects
    # bar / hbar by label length (rule 11). Taking over loses that decision; device sampling
    # found three comparisons with long brand names regressing from hbar to bar. Handle only the cases the shape tree misses:
    # rule 3 unconditionally consumes geo dimensions as bar, preventing them from reaching rule 10's dual axis.
    if norm_hint == "compare" and dims and len(num_cols) >= 2 and n <= _MAX_BAR_ROWS and n_axes == 2:
        return "dual_axis", f"hint=compare + {len(num_cols)} num → 2 量纲组 → dual_axis"

    # Composition stacking -> stacked bars. Both multiple-dimension breakdowns and one dimension with multiple components qualify.
    # Apply the unit-family gate here too: stacking makes sense only on one axis. Stacking revenue with profit margin adds
    # incompatible units, producing a height with no meaning.
    if (
        norm_hint == "composition"
        and n <= _MAX_BAR_ROWS
        and n_axes == 1
        and ((len(dims) >= 2 and num_cols) or (len(dims) == 1 and len(num_cols) >= 2))
    ):
        return "stacked_bar", f"hint=composition + {len(dims)} dim + {len(num_cols)} num → stacked_bar"

    # ── The next four branches were decision-tree rules 5b/5c/6/7. Their criteria were moved **unchanged**, including
    #    using `cat_cols` rather than `dims`, leaving two dimension definitions in one function:
    #    the new branches recognize `省/市` (geo is a dimension), while the four moved branches do not.
    #
    #    116 dogfood finding (2026-09-01): `各省营业额占比` produced bars instead of a pie.
    #    Corpus rerun: **13 of 42 share calls fully met the pie shape but were blocked by geo dimensions**
    #    (only five currently pass); composition similarly blocks five.
    #    bridge / flow currently have no impact, but share the inconsistency (province->city Sankey would be blocked).
    #    Unify all four; leaving half unchanged preserves a known defect. ──

    if norm_hint == "bridge" and dims and len(num_cols) == 1 and 2 <= n <= 20:
        return "waterfall", f"hint=bridge + 1 dim + 1 num + n={n} → waterfall"

    if norm_hint == "flow" and len(dims) >= 2 and len(num_cols) >= 1 and n <= 60:
        return "sankey", f"hint=flow + {len(dims)} dim + num + n={n} → sankey"

    if norm_hint == "share" and dims and len(num_cols) == 1 and _MIN_PIE_ROWS <= n <= _MAX_PIE_ROWS:
        return "pie", f"hint=share + n={n} in [{_MIN_PIE_ROWS},{_MAX_PIE_ROWS}] → pie"

    if norm_hint == "correlation" and len(num_cols) >= 2:
        return "scatter", f"hint=correlation + {len(num_cols)} numeric → scatter"

    return None


def infer_chart_spec(
    rows: list[dict[str, Any]],
    columns: list[ChartColumn],
    *,
    hint: str | None = None,
    title: str = "数据图表",
    subtitle: str | None = None,
    data_artifact_id: str | None = None,
    force_kind: str | None = None,
    inline_rows_limit: int = 50,
) -> ChartSpec:
    """推断 ChartSpec. rows 为 parsed CSV 行；columns 类型已事先识别.

    行数超上限时先取 top-N 再进判定树（见 `_take_top_n`）。截断**只在这一层做**：
    判定树里有十来个 `return _make_spec(...)`，逐个传截断标记一定会漏，而漏一个就是
    「截了但没告诉用户」——比不截更糟。所以压行、还原总量、打标记都收在这个单一出口，
    内部判定树一行不用改，将来新增分支也自动被兜住。

    Args:
        rows: 全量行数据（numeric 列已 cast 成 float/int）
        columns: 列元信息；type 字段必填
        hint: LLM 给的粗粒度偏好
        title / subtitle: 展示标题
        data_artifact_id: 长表走 artifact 引用（前端 hydrate 时 fetch）
        force_kind: 上层显式强制（override 推断；罕用）
        inline_rows_limit: **表格**内联的行数（其余靠「展开全部」去文件补）。
            图不受它管，见 `_CHART_ROWS_MAX`。

    Returns:
        ChartSpec
    """
    norm_hint = hint.strip().lower() if hint else "raw"
    if norm_hint not in VALID_HINTS:
        norm_hint = "raw"

    # Detect constant dimensions once using **all** rows; routing and column ordering share the result. See `_sink_constant_dims`.
    const_dims = _constant_dim_keys(rows, columns)
    columns = _sink_constant_dims(columns, const_dims)

    full_rows = rows
    total_rows = len(rows)
    n_series = sum(1 for c in columns if c.type == "numeric")
    display_cap = _display_row_cap(norm_hint, n_series)
    # A date column constant across rows is not a time axis (`每月=2026年8月` is a filter trace), and must not bypass truncation.
    has_date = any(c.type == "date" and c.key not in const_dims for c in columns)
    top_n: int | None = None
    # Do not truncate time axes: value-based top-N removes arbitrary months from a time series, which is meaningless
    # (rule 4 enforces the line-family `_MAX_LINE_POINTS` cap by time points, not rows).
    if not has_date and total_rows > display_cap:
        rows = _take_top_n(rows, [c for c in columns if c.type == "numeric"], display_cap)
        top_n = len(rows)

    spec = _infer_from_rows(
        rows,
        columns,
        norm_hint=norm_hint,
        title=title,
        subtitle=subtitle,
        data_artifact_id=data_artifact_id,
        force_kind=force_kind,
        inline_rows_limit=inline_rows_limit,
        const_dims=const_dims,
    )
    # Undo truncation for **tables**: the 30-row cap limits readable bars, which tables do not need
    # (they have their own pagination: footer `显示 1-30 / 共 38` plus Expand all).
    #
    # ⚠ Actual 116 acceptance regression (session s_c88220d81f9e): model `force_kind="table"`,
    # title `All 38 Category Classes`, yet only 30 rows remained. It explicitly requested the whole table; I gave three quarters.
    # The old code used `n>30 -> table` and inlined all 38 rows. Moving truncation **before** chart-kind selection
    # introduced this regression.
    #
    # Undoing is safe: table criteria (unit families / series count / cat>=3 / no usable values / fallback)
    # **do not depend on row count**, so the untruncated shape remains a table without rerunning the tree.
    # The same applies to `hbar`: categories are on the **vertical axis**, and the container grows with category count, scrolling inside
    # when needed (`.v2-chart-canvas-wrap.is-tall`). More rows do not hurt readability. Vertical bars and the line family differ:
    # their categories are on the horizontal axis, where extra rows crowd together and still need truncation.
    if spec.kind in _ROWS_SCROLLABLE_KINDS and top_n is not None:
        if total_rows <= inline_rows_limit:
            restored = full_rows
        elif spec.kind in ("table", "scatter"):
            # Tables take the first N rows in **original order**, matching the footer `显示 1-50 / 共 120`;
            # agent-provided row order may itself be meaningful (no ranking was requested).
            #
            # Scatter also keeps original order, **deliberately bypassing top-N below**: taking the largest N points
            # removes an entire low-value region on one axis and produces a biased cloud, precisely the defect
            # found this time. Original order at least avoids coordinate-based bias. None of the 20 corpus scatter charts exceeded 50 rows,
            # so this branch is currently unreachable in practice; it prevents repeating the defect if reached later.
            restored = full_rows[: inline_rows_limit if spec.kind == "table" else _CHART_ROWS_MAX]
        else:
            # Horizontal bars are rankings and must select the first N **by value**.
            # ⚠ This previously sliced like tables, plotting the **smallest 50** when original rows were ascending:
            # the repeatedly documented false Top10 produced by ranking only half the data.
            restored = _take_top_n(full_rows, [c for c in columns if c.type == "numeric"], inline_rows_limit)
        spec.rows = restored
        # Who reports omitted rows differs by kind:
        #   table          -> **The footer already says** `显示 1-50 / 共 120`; adding `top_n` repeats
        #                     the fact with `仅显示前 50 项` below the chart. Always clear it.
        #   hbar / scatter -> **There is no footer**; omitting the marker conceals that only 50/9183 rows were plotted.
        #                     Set it from the actual plotted row count, rather than always clearing it.
        #                     Restoring all rows (`len(restored) >= total_rows`) naturally yields None;
        #                     this is the main repaired path: all 39 rows are shown, so do not claim only the first 30.
        top_n = None if spec.kind == "table" or len(restored) >= total_rows else len(restored)
    elif spec.kind != "table":
        # Charts include every row to be plotted (see `_CHART_ROWS_MAX`). `rows` already contains that set:
        # category axes were reduced by top-N; time axes remain complete. Branch-level `inline_rows_limit` slices
        # from the decision tree are discarded here because they use the table convention.
        if spec.kind == "hbar":
            # Horizontal bars with date columns bypass the restoration block above (time axes skip top-N, leaving `top_n` empty);
            # likewise select the top `inline_rows_limit` bars by value. See `_CHART_ROWS_MAX` for the rationale.
            spec.rows = _take_top_n(rows, [c for c in columns if c.type == "numeric"], inline_rows_limit)
        else:
            spec.rows = rows[:_CHART_ROWS_MAX]
        if len(spec.rows) < total_rows:
            top_n = len(spec.rows)
        if spec.kind == "dual_axis":
            # Axis assignment previously used `_make_spec`'s first 50 inline rows. Charts now plot all rows, so different later-row scales
            # (such as visitor counts surging after row 50) would assign wrong axes and flatten the smaller series near zero.
            kind, spec.columns, reason = _resolve_dual_axis(spec.rows, spec.columns, spec._infer_reason or "")
            if kind != spec.kind:
                spec.kind, spec._infer_reason = kind, reason
        if spec.kind == "heatmap":
            sparse = _sparse_heatmap_reason(spec.rows or [], spec.columns)
            if sparse is not None:
                spec.kind, spec._infer_reason = "stacked_bar", f"{spec._infer_reason} → {sparse}"
        if spec.kind == "stacked_bar":
            # Declare which dimension is the category axis and which is the series (see `_resolve_stack_dims`).
            # Decided here, once, for **every** stacked producer (hierarchy rule 8a, intent `composition`, `force_kind`)
            # and on the rows actually plotted: `_make_spec` only sees the first 50 inline rows, and a 50-row slice of
            # crossed dimensions (59 provinces in one month) looks like a hierarchy.
            kind, spec.columns, note = _resolve_stack_dims(spec.rows, spec.columns)
            if note:
                spec.kind, spec._infer_reason = kind, f"{spec._infer_reason} → {note}"
        # The series gate runs **before both series-count gates**: first take the top N by total, then `_undrawable_reason`
        # asks whether this already-reduced chart can be drawn. Reversing the order classifies 59 provinces as drawable
        # (59 < 100) and plots a ribbon, while 1070 brands fall back to a table before getting a chance to select the top 15.
        #
        # ⚠ Deliberately **leave `top_n` unchanged**: it means only the first N **rows** are plotted, and the time-series truncation
        # fallback below uses that field. Setting it for series truncation would immediately turn the chart into a table,
        # defeating this gate. Series counts use `series_shown/series_total`.
        spec, reusable_shape = _cap_time_series(spec, const_dims)
        # Too many time points -> downsample rather than fall back to a table (see `_downsample_time_axis`). Call unconditionally:
        # it changes no bytes when the time count is within the cap, at the cost of one row scan.
        drawn_rows_before = len(spec.rows or [])
        spec, sampled_shape = _downsample_time_axis(spec, const_dims, _MAX_LINE_POINTS)
        if sampled_shape is not None:
            reusable_shape = sampled_shape  # The spec did not change, so this normalization avoids another call inside `_undrawable_reason`.
        # Apply the same byte-budget approach: time-axis line charts drop **points**, not entire time ranges. Estimate a proportional target time count,
        # leaving 5% headroom (row byte sizes vary); `_fit_rows_to_bytes` below handles anything still too large.
        if spec.kind in _DOWNSAMPLE_KINDS:
            over = _rows_bytes(spec.rows or [])
            if over > _CHART_BYTES_MAX:
                date_cols = [c for c in spec.columns if c.type == "date" and c.key not in const_dims]
                now = spec.points_shown or _time_points(spec.rows or [], date_cols)
                # ⚠ The lower bound is not 3: with only single-digit points, charts have just a few bends, while all three consumers still claim
                # extrema and endpoints are retained. That no longer holds for **each series**: aggregated series can hide smaller-series spikes,
                # as review measured with A noise ±100,000 and B spikes of 500.
                # Prefer the existing complete-table fallback over a misleading sparse chart.
                target = int(now * _CHART_BYTES_MAX * 0.95 / over)
                if target >= _MIN_DOWNSAMPLE_POINTS:
                    spec, byte_shape = _downsample_time_axis(spec, const_dims, target)
                    if byte_shape is not None:
                        reusable_shape = byte_shape
        if len(spec.rows or []) != drawn_rows_before:
            # The spec changed, invalidating the normalization above.
            # ⚠ State the limitation honestly: this is **currently unreachable**. `_cap_time_series` returns a shape only when dimension cardinality >15
            # but plotted series count ≤15. Those shapes have slot counts different from time counts,
            # so the downsampling guard rejects them (zero hits across five measured shapes). Keep this because relaxing either guard later
            # would otherwise reuse stale shape data and reject a freshly downsampled chart as having too many points.
            reusable_shape = None
        undrawable = _undrawable_reason(spec, reusable_shape)
        if undrawable is not None:
            spec = _make_spec(
                kind="table",
                title=title,
                subtitle=subtitle,
                rows=full_rows[:inline_rows_limit],
                row_count=total_rows,
                columns=columns,
                data_artifact_id=data_artifact_id,
                reason=f"{spec._infer_reason} → {undrawable}（画不了 / 导出冻结不下）→ table",
            )
            top_n = None
    # Centralize the byte gate here for every chart kind, including restored horizontal bars/scatter: row caps cannot bound wide tables or long text.
    # Tables omit `top_n` (the footer says `显示 1-N / 共 M`); other truncated chart kinds must report it.
    fitted = _fit_rows_to_bytes(spec.rows or [], _CHART_BYTES_MAX)
    if fitted is None:
        # Not even one row fits -> table fallback: its footer reports the total rows, and readers can open the complete data.
        spec = _make_spec(
            kind="table",
            title=title,
            subtitle=subtitle,
            rows=full_rows[:1],
            row_count=total_rows,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=f"{spec._infer_reason} → 单行就超过 {_CHART_BYTES_MAX}B（画不出）→ table",
        )
        top_n = None
    elif len(fitted) < len(spec.rows or []):
        spec.rows = fitted
        if spec.kind != "table":
            top_n = len(fitted)

    # A time-axis chart that still cannot fit becomes a complete-data table.
    # Ranking by value or slicing row order can remove an entire time interval;
    # prefer table expansion and download over a misleading partial trend.
    if top_n is not None and spec.kind in _TIME_AXIS_KINDS and any(c.type == "date" and c.key not in const_dims for c in columns):
        spec = _make_spec(
            kind="table",
            title=title,
            subtitle=subtitle,
            rows=full_rows[:inline_rows_limit],
            row_count=total_rows,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=f"{spec._infer_reason} → 时间序列装不下（{total_rows} 行），截一段时间没有意义 → table",
        )
        top_n = None

    # Two unit families on one axis flatten the smaller series near zero. Upgrade to dual axis; see `_upgrade_single_axis`.
    spec = _upgrade_single_axis(spec, rows or [])
    # ★ Truncated rows must no longer be stacked, following the same principle as `_cap_time_series`:
    # stacked height is read as a total, and an incomplete total is false and worse than no chart.
    # That guard handles only **series** truncation; row truncation was never connected.
    #
    # 116 measured `t_664e64068487` (1.3.56): two charts in one report contradicted each other:
    #   Pie:     Shanghai 37.71 / Beijing 36.64 / Jiangsu 17.48 / Tianjin 17.39 / Henan 11.17 hundred million (total 120.39).
    #   Stacked: Shanghai 26.66 / Beijing 24.09 / Jiangsu  6.65 / Tianjin  6.30 / Henan  1.64 hundred million (total  65.35).
    # `top_n=30` cut 451 rows to 30, **reducing overall bar height by 46% with unequal losses by province**;
    # Henan's share fell from 9.3% to 2.5%, making even relative heights unreliable.
    # The caption `仅显示前 30 项 · 共 451 项` honestly describes **row count**, but users read **bar height**.
    #
    # Removing stacking gives each segment its own bar/line, whose height represents only itself without pretending to be a total.
    # Deliberately **do not synthesize Other**: that requires additivity, which could only be guessed from column names;
    # follow `_cap_time_series`'s tradeoff and never fabricate values.
    #
    # Impact: **only one** of 31 stacked charts in the whole database had truncated rows (the one above lost 93% of rows);
    # actual parent/child stacked charts all stayed within the row cap.
    if top_n is not None and spec.kind in _STACKED_KINDS:
        _was = spec.kind
        spec.kind = "line" if _was == "area_stacked" else "bar"
        spec._infer_reason = f"{spec._infer_reason} → 行被截到 {top_n}/{total_rows}，堆叠的高度不再是真合计 → {spec.kind}"
    # Truncation changes **presentation**, not totals. Restore the true total to `row_count`, matching `applyView`.
    spec.row_count = total_rows
    spec.top_n = top_n
    return spec


# When an axis's maximum/minimum scale ratio exceeds this, the smaller series becomes a flat line near zero.
_FLAT_SPAN = 100.0

# Only these two kinds can upgrade to dual axis. **Exclude hbar**: `buildDualAxis` has no horizontal mode, so upgrading loses
# the reason for horizontal bars: long category labels (this sprint just fixed category-count-based hbar height;
# do not undo it). Exclude stacked_bar too: summing incompatible units is a different defect
# that changing axes cannot fix.
#: Chart kinds whose bar/area heights **are read as totals**; truncated rows make those heights false.
_STACKED_KINDS = frozenset({"stacked_bar", "area_stacked"})

_UPGRADABLE_TO_DUAL = frozenset({"line", "bar"})

# Chart kinds with multiple numeric series **sharing one value axis**. Multiple unit families make the smaller series invisible near zero.
# This differs from `_UPGRADABLE_TO_DUAL`, which decides whether an automatically selected single-axis chart can upgrade in place
# (hbar is excluded because frontend `buildDualAxis` lacks horizontal mode). This controls whether `force_kind`
# can override the shape tree's dual-axis decision; denying the override uses the shape tree's output rather than an in-place upgrade,
# so hbar is included.
# Exclusions: kpi/pie (one series), dual_axis (already two axes), scatter (separate x/y axes),
# heatmap/waterfall/sankey/geo/table (not multiple series on a shared axis).
_SHARED_VALUE_AXIS_KINDS = frozenset({"line", "bar", "hbar", "stacked_bar", "combo", "area_stacked"})

# Chart kinds requiring **named** category/x-axis ticks: without dimension columns (cat/date/geo), no labels can render.
#
# Excluded kinds each have their own reason:
#   `scatter`: x/y are inherently numeric (`hint=correlation + 2 numeric -> scatter` is the intended path);
#   `heatmap` / `sankey` / `geo`: separate shape requirements, outside the single-category-axis family;
#   `kpi` / `table`: no axes.
_LABEL_NEEDING_KINDS = frozenset({"line", "bar", "hbar", "stacked_bar", "area_stacked", "combo", "dual_axis", "waterfall", "pie"})


def _upgrade_single_axis(spec: ChartSpec, rows: list[dict[str, Any]]) -> ChartSpec:
    """单轴图上混了两个量纲族、且小的那条已经贴零 ⇒ 改出双轴。

    ## 为什么会漏

    `n_axes = len(_axis_groups(...))` 早就算出来了，但**只有 `_kind_from_intent` 的
    `hint=compare` 那一条分支在读**。形状树（规则 4 时间→line、规则 11 类目→bar）
    一路不看量纲族，于是「营业额 + 利润率」这种组合只要没带 `hint=compare`
    就落成单轴——而 hint 本身 92.3% 是空的。又一个「算出来了没人读」。

    ## 语料实测（116，1877 张，端到端过 builder 量的）

    渲染出 ≥2 条序列的图 416 张，其中**有一条贴零看不见的 121 张（29%）**。
    典型：
      `2026 Revenue & Profit by Category`  营业额+利润 / 利润率  跨度 39 亿倍
      `2026年各月营业额同比变化率`          标题就是「变化率」，而那条正是看不见的
      `华东月度营业额&客流趋势`             营业额+利润 / 客流量  跨度 752 倍

    ## 判据里为什么要带「已经贴零」

    只有 ≥2 族不够：两个族量级相近时（客单价 元 / 平均件数）一根轴画得好好的，
    再劈一根纯属添乱。带上跨度闸只动真出问题的那些。
    """
    if spec.kind not in _UPGRADABLE_TO_DUAL:
        return spec
    num_cols = [c for c in spec.columns if c.type == "numeric"]
    if len(num_cols) < 2 or not rows:
        return spec
    # ⚠ This once had a >60-row no-upgrade guard to avoid dense left-axis bars. It now has **no reachable role**:
    # category axes are already capped at ≤30 rows by `_display_row_cap`; only trend charts exceed 60 on time axes.
    # Refusing to upgrade a trend chart makes **the ratio completely invisible** (365 days of revenue 1e9 + profit margin 0.01
    # on one axis gives a flat line near zero; the old >200-row table fallback masked this until the row cap was relaxed).
    # Denser bars are better than an invisible line; `_MAX_LINE_POINTS` governs how many horizontal-axis points fit.
    if len(_axis_groups(rows, num_cols)) < 2:
        return spec
    mags = [m for m in (_column_magnitude(rows, c) for c in num_cols) if m > 0]
    if len(mags) < 2 or max(mags) / min(mags) < _FLAT_SPAN:
        return spec
    kind, columns, reason = _resolve_dual_axis(
        rows,
        spec.columns,
        f"{spec._infer_reason or spec.kind} → 两个量纲族且跨度 {max(mags) / min(mags):.0f}x，升双轴",
    )
    spec.kind = kind
    spec.columns = columns
    spec._infer_reason = reason
    return spec


_DIM_TYPES = ("date", "categorical", "geo")


def _constant_dim_keys(rows: list[dict[str, Any]], columns: list[ChartColumn]) -> set[str]:
    """整列同值的维度列 = filter 痕迹，不是真维度。

    实测：`bi_fetch_rows` 的 query 带 filter（`审批状态=END_APPROVAL`）会把 filter
    字段一并带回来，整列同值；沙箱产出的表也常把「过滤维度」排在「分组维度」前面
    （`产品名称=爱必妥`、`每月=2026年8月`）。

    **至少要留下一个能区分行的维度**，否则返回空集——全常量时没有更好的候选，
    该由 `indistinguishable_rows_reason` 守卫去报警，不是在这里悄悄改形状。
    """
    if not rows:
        return set()
    dims = [c for c in columns if c.type in _DIM_TYPES]
    if len(dims) < 2:
        return set()
    const = {c.key for c in dims if len({str(r.get(c.key)) for r in rows}) <= 1}
    return set() if len(const) >= len(dims) else const


def _rows_bytes(rows: list[dict[str, Any]]) -> int:
    return len(json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _fit_rows_to_bytes(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]] | None:
    """把 `rows` 压到 `limit` 字节以内（见 `_CHART_BYTES_MAX`）。

    返回压过的行；**单独一行就超预算**时返回 `None`——那种数据（某个单元格塞了整篇文本）
    画不了，交给调用方退表，而不是留一行假装画得出：复审实测旧写法把一张 1 万点的图
    削成 1 个点，还报「仅显示前 1 项」。

    二分收缩、总量只算一次：1 万行逐轮序列化最坏要 177ms，而这是在事件循环上同步跑的。
    """
    if not rows or _rows_bytes(rows) <= limit:
        return rows
    if _rows_bytes(rows[:1]) > limit:
        return None
    lo, hi = 1, len(rows)  # lo always fits; hi never fits.
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _rows_bytes(rows[:mid]) <= limit:
            lo = mid
        else:
            hi = mid
    return rows[:lo]


def _not_a_flow_reason(rows: list[dict[str, Any]], columns: list[ChartColumn]) -> str | None:
    """Return why disconnected Sankey rows cannot represent a flow, or None.

    Independent stage-to-label bands compare widths without connecting stages and may mix incompatible units.
    Reject only when no node is shared by two positive links. Do not require flow conservation: entries outside
    the supplied data can legitimately make an intermediate node emit more than it receives. Connected mixed-unit
    flows remain undetected. Mirror frontend notAFlowReason with the same dimensions and positive-flow filter.
    """
    dims = [c for c in columns if c.type in ("categorical", "date")]
    flow_col = next((c for c in columns if c.type == "numeric"), None)
    if len(dims) < 2 or flow_col is None:
        return None
    degree: dict[str, int] = {}
    links = 0
    for r in rows:
        src, dst, flow = r.get(dims[0].key), r.get(dims[1].key), r.get(flow_col.key)
        if src in (None, "") or dst in (None, ""):
            continue
        if isinstance(flow, bool) or not isinstance(flow, (int, float)) or not flow > 0:
            continue
        links += 1
        for node in (str(src), str(dst)):
            degree[node] = degree.get(node, 0) + 1
    if links and all(d == 1 for d in degree.values()):
        return f"Sankey links share no node ({links} independent bands) — not a flow, widths would compare unrelated rows"
    return None


def _undrawable_before_normalizing(spec: ChartSpec) -> str | None:
    """跑正规化**之前**就能下的结论——正规化本身就是开销所在，谁要跑它谁先过这里。

    ⚠ 这两条必须先于正规化：稀疏堆叠柱（10000 行 × 5000 个 SKU）正规化要把整个
    类目×分组矩阵物化出来，几十秒、四百多 MB，而且是在 gateway 事件循环上**同步**算的。
    本函数从 `_undrawable_reason` 里抽出来，就是因为 `_cap_time_series` 也要跑正规化——
    它第一版直接跑，把那 33 秒的阻塞原样放了回来（单测当场逮到）。
    """
    if spec.kind == "heatmap":
        # Match frontend `build-config.ts` `pickAxes`: it recognizes categorical/date, **not geo**.
        # Counting `_DIM_TYPES` measures the wrong column when geo is present, as review found.
        dims = [c for c in spec.columns if c.type in ("categorical", "date")]
        if len(dims) >= 2:
            y_values = len({str(r.get(dims[1].key)) for r in spec.rows or []})
            if y_values > _MAX_HEATMAP_Y:
                return f"热力图纵轴 {y_values} 个类目 > {_MAX_HEATMAP_Y}（每格压到 1px，图上一片空白）"

    if spec.kind == "sankey":
        not_a_flow = _not_a_flow_reason(spec.rows or [], spec.columns)
        if not_a_flow is not None:
            return not_a_flow

    if spec.kind == "stacked_bar":
        dims = [c for c in spec.columns if c.type in _DIM_TYPES][:2]
        if len(dims) == 2:
            cells = math.prod(len({str(r.get(c.key)) for r in spec.rows or []}) for c in dims)
            if cells > _MAX_STACK_CELLS:
                return f"堆叠 {cells} 格（类目×分组）> {_MAX_STACK_CELLS}"
    return None


_NATURAL_CHUNKS = re.compile(r"(\d+)")


def _date_axis_sort_key(value: str) -> tuple[tuple[int, int, str], ...]:
    """时间轴标签的排序键——数字段按**数值**比，不按文字比。

    ⚠ 每个块都是**同构**的三元组 `(档位, 数值, 文本)`，不能直接混 `int` 和 `str`：
    `("2025-01", "合计")` 这种一比就 `TypeError: '<' not supported between 'str' and 'int'`。
    第一版正是这么写的，后果不是排错序而是**整张图崩掉**——13 行的普通月度走势，
    末尾多一行「合计」就炸，而 `add_chart` 没有兜底、自动出图那条会把异常吞掉
    ⇒ 用户看到的是「本来有图现在没了」。透视表尾行（合计/小计）、SQL NULL 日期
    都会让列类型仍被推断成 `date`（门槛是前 20 个样本 ≥80% 像日期）。

    档位：`0` 数字块 < `1` 文本块 < `2` 空值。空值排最后是跟前端对齐的
    （`compareDateAxisValues` 显式把 `""`/`null` 排到末尾）。

    ## 与前端的关系

    前端 `lib/chart/date-order.ts` 用 `Intl.Collator("en", {numeric: true, sensitivity: "base"})`。
    两边由 `test_date_axis_order_matches_the_frontend` / `date-order.test.ts` 用**同一组标签**对拍。
    39 组抽样里 36 组一致，**已知三处不跟**（都不会出现在时间轴标签上，刻意不追 ICU 全等）：
      · 重音：`cafe` vs `café` —— ICU 的 `sensitivity:"base"` 视为相等，`casefold()` 不是；
      · 负号：`-10` vs `5` —— ICU 认负数，这里 `-` 只是个文本块；
      · `_` 与 `-` 的权重顺序相反。

    ★ 它只用来决定**降采样挑哪些点**（LTTB 必须沿着画出来的那个顺序看形状）。
    画出来的顺序由前端定，后端正规化不排序——那个分歧是 `957db91e3` 引入的既有状态，
    不在本批范围内。
    """
    if value is None or value == "" or value == "None":
        return ((2, 0, ""),)
    return tuple((0, int(c), "") if c.isdigit() else (1, 0, c.casefold()) for c in _NATURAL_CHUNKS.split(value) if c != "") or ((2, 0, ""),)


def _lttb_indices(values: list[float], target: int) -> list[int]:
    """LTTB（Largest-Triangle-Three-Buckets）：从 `values` 里挑 `target` 个**下标**。

    把序列切成 `target` 个桶，逐桶选出「与前一个已选点、下一桶均值构成的三角形面积最大」
    的那个点。面积大 ≈ 这个点偏离前后趋势线远 ⇒ 平直段上的点被丢掉、拐点与尖峰被留下。
    首尾两点固定保留。

    为什么是它而不是别的（3500 点埋 3 个业务尖峰，降到 1000 点实测）：
        朴素每 N 取 1   尖峰保 0/3，画出的最大值 −50.3%
        **LTTB**        尖峰保 3/3，画出的最大值 **±0.0%**
        分桶平均        尖峰保 1/3，−45.5%
        分桶求和        尖峰保 1/3，**+118.1%**（降到 500 点时 +279.5%）
    分桶聚合还要求知道这一列**可不可加**——金额可加，利润率 / 退款率 / 占比不可加，
    而判可加性只能回到按列名猜，正是本仓在百分比形状上栽过七次的那条路。
    LTTB 不需要知道，而且画出来的每个数都还是**真实观测值**（tooltip 不说谎）。

    与 Chart.js 内置 decimation 插件、Power BI 高密度采样同一族做法。

    ⚠ 单测钉的是**契约**（极值与首尾保留、下标保持时间序、画出来的都是真实观测值），
    不是这段算术本身：把三角形面积换成「与前一个选中点的 |Δy|」，四种形状实测极值表现相同
    （噪声那档甚至略好），用例不会红。这是**有意**的——用户看得见的就是那几条契约。
    真正要挡住的失败形态（朴素每 N 取 1，尖峰全丢、峰值 −50%）由负对照钉死。
    """
    n = len(values)
    if target >= n or target < 3:
        return list(range(n))
    picked = [0]
    every = (n - 2) / (target - 2)
    a = 0
    for i in range(target - 2):
        lo = math.floor((i + 1) * every) + 1
        hi = min(math.floor((i + 2) * every) + 1, n)
        if hi > lo:
            avg_x = (lo + hi - 1) / 2
            avg_y = math.fsum(values[lo:hi]) / (hi - lo)
        else:
            avg_x, avg_y = float(n - 1), values[-1]
        r_lo = math.floor(i * every) + 1
        r_hi = min(math.floor((i + 1) * every) + 1, n)
        best, best_area = r_lo, -1.0
        for j in range(r_lo, r_hi):
            area = abs((a - avg_x) * (values[j] - values[a]) - (a - j) * (avg_y - values[a])) / 2
            # Strictly greater: ties choose the **lower index**. Both choices are deterministic (this scans a list sequentially,
            # without dict iteration-order concerns); freeze it for reproducibility across versions so identical data always selects identical points.
            if area > best_area:
                best_area, best = area, j
        picked.append(best)
        a = best
    picked.append(n - 1)
    return picked


def _downsample_time_axis(spec: ChartSpec, const_dims: set[str], target_points: int) -> tuple[ChartSpec, dict[str, Any] | None]:
    """Downsample a continuous time axis with LTTB while preserving its span.

    Only line-family kinds may be sampled; sparse bar spacing would be misleading.
    Run LTTB once on the sum of absolute series values at each timestamp and retain
    all series at the chosen timestamps, keeping their x coordinates aligned.
    Always retain the first and last timestamps.
    """
    if spec.kind not in _DOWNSAMPLE_KINDS or target_points < 3:
        return spec, None
    rows = spec.rows or []
    date_cols = [c for c in spec.columns if c.type == "date" and c.key not in const_dims]
    num_cols = [c for c in spec.columns if c.type == "numeric"]
    if len(date_cols) != 1 or not num_cols or not rows:
        return spec, None

    key = date_cols[0].key
    agg: dict[str, float] = {}
    for r in rows:
        slot = str(r.get(key))
        acc = agg.get(slot, 0.0)
        for c in num_cols:
            v = r.get(c.key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                acc += abs(float(v))
        agg[slot] = acc
    slots = sorted(agg, key=_date_axis_sort_key)
    if len(slots) <= target_points:
        return spec, None

    # Act only when the plotted horizontal-axis slots **are exactly** these time points, following the series-gate principle:
    # what is counted must be what is drawn. An unpivotable long table (insufficient fill / two numeric columns)
    # uses one slot per row. 3200 days × 20 brands gives 64,000 slots; downsampling to 3000 days
    # still gives 3000 × 20 = 60,000 slots and falls back to a table.
    #
    # ⚠ Honest limitation: this guard **does not change the visible result** (the chart becomes a table either way;
    # mutation checks passed both variants). It saves pointless downsampling and avoids marking a soon-discarded spec
    # with `points_shown`. The `categoryCount` comparison above ensures the correct object is counted.
    #
    # Normalize only **when actually needed** (after `len(slots) > target`), using the same optimization as `_cap_time_series`.
    if _undrawable_before_normalizing(spec) is not None:
        return spec, None

    from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1

    _, shape = normalize_chart_for_rendering_v1(spec_to_dict(spec))
    if int(shape.get("categoryCount") or 0) != len(slots):
        return spec, shape

    keep = {slots[i] for i in _lttb_indices([agg[s] for s in slots], target_points)}
    # Enforce the contract to populate this **only after actual downsampling** here, without relying on incidental arithmetic in earlier gates:
    # marking unchanged time points makes all three consumers falsely claim N total points were downsampled to N,
    # and the frontend stops filling rows because `points_shown != null`.
    if len(keep) >= len(slots):
        return spec, shape
    spec.rows = [r for r in rows if str(r.get(key)) in keep]
    spec.points_shown = len(keep)
    # ⚠ Record the total only on the first downsampling pass: fitting the byte budget may require another pass,
    # whose `slots` are already sampled. Overwriting would understate the original time-point count.
    spec.points_total = spec.points_total or len(slots)
    spec._infer_reason = f"{spec._infer_reason} → 时间点 {len(slots)} 个装不下 → LTTB 按合计序列降采样到 {len(keep)} 个（首尾保留）"
    return spec, None


def _cap_time_series(spec: ChartSpec, const_dims: set[str]) -> tuple[ChartSpec, dict[str, Any] | None]:
    """时间轴上画出来的系列超过 `_MAX_TIME_SERIES` 时，按合计值取前 N 条——其余不画但明示。

    ## 为什么不合并成「其余」

    合并要求这一列**可加**。金额可加，而「利润率 / 退款率 / 占比」不可加——59 个省的
    利润率相加是垃圾。判可加性只能回到按列名猜，那条路本仓在百分比形状上栽过七次。
    所以只取前 N、**不编造值**，并把「共几条、画了几条」写进 spec 让三个消费方照实说。
    `_take_top_n` 的尾巴处理（丢弃而不是折成「其余」）就是同一个取舍，这里保持一致。

    ## 为什么截断之后不能再堆叠

    堆叠图的**高度读作「合计」**。缺了 44 个省的合计是假的合计，比不画更糟。
    所以「截断」与「堆叠」互斥：一旦截，`area_stacked` 改画 `line`。
    副作用是长表路径不再产出 `area_stacked`——它今天**只在 `uniq > 15` 时触发**，
    而那一档现在一律截到 15 条线。不是删功能，是删掉一条从来产不出好图的分支。

    返回 `(spec, shape)`：`shape` 非空表示 **spec 一个字节都没动**，
    调用方可以直接把这份正规化结果交给 `_undrawable_reason`，不必对同一份输入再算一遍
    （复审实测：18000 行那档 106ms → 209ms，纯重复）。

    ## 判据复用

    系列数**直接问 `_undrawable_reason` 用的那一份正规化**，不在这里另写「谁摊成系列」：
    长表只有在摊得开时（1 个数值列 + 2 个维度 + 填充率够）才是「一个维度值一条系列」，
    摊不开时一行一个槽位、`seriesCount` 就是数值列数，这时截维度值只会白丢数据。
    上一轮我自己写过一份 pivot 规则，数错了（见 `_undrawable_reason` 的注释）。
    """
    if spec.kind not in _TIME_AXIS_KINDS:
        return spec, None
    rows = spec.rows or []
    num_cols = [c for c in spec.columns if c.type == "numeric"]
    dims = [c for c in spec.columns if c.type in ("categorical", "geo") and c.key not in const_dims]
    if not rows or not num_cols or not dims:
        return spec, None

    # Total-value table for each candidate dimension. Rank by the sum across **all numeric columns**, not just the first:
    # the transpose path (one dimension + multiple time-header columns, `_transpose_time_wide_spec`) uses one series per row,
    # whose total is the horizontal row sum. Review found first-column-only ranking retained the **complement** of the correct result
    # (30 provinces, zero overlap), while the caption explicitly claimed the top 15 by total.
    # Sum absolute values: an all-negative profit series can still be one of the chart's most important lines.
    per_dim: dict[str, dict[str, float]] = {}
    for c in dims:
        totals: dict[str, float] = {}
        for r in rows:
            acc = totals.get(str(r.get(c.key)), 0.0)
            for nc in num_cols:
                v = r.get(nc.key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    acc += abs(float(v))
            totals[str(r.get(c.key))] = acc
        per_dim[c.key] = totals
    # ⚠ Check whether any dimension needs truncation before normalization, which takes tens of milliseconds on tens of thousands of rows;
    # most charts already have dimension cardinality ≤15.
    if all(len(v) <= _MAX_TIME_SERIES for v in per_dim.values()):
        return spec, None

    # Run this cheap precheck before normalization: sparse stacked-bar normalization takes tens of seconds and hundreds of MB,
    # synchronously on the event loop. Rejected charts fall back to tables anyway, so do not compute their shape again.
    if _undrawable_before_normalizing(spec) is not None:
        return spec, None

    from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1

    _, shape = normalize_chart_for_rendering_v1(spec_to_dict(spec))
    drawn = int(shape.get("seriesCount") or 0)
    if drawn <= _MAX_TIME_SERIES:
        return spec, shape
    # ★★ Modify only the dimension **proven to be the series axis**: its cardinality must exactly equal the plotted series count.
    # Counting only dimension values truncates the wrong object: the bar family selects its x axis by **column order**;
    # pivoting `(门店, 日期, 金额)` gives one series per day (30), but dimension selection recognizes only categorical/geo,
    # always choosing stores (20). Review found three simultaneous errors: silently dropping five stores / 150 rows,
    # **still plotting 30 series** with no readability improvement, and telling all three consumers there were 20 series.
    #
    # This criterion also resolves two other issues:
    #   · Unpivotable long tables (one slot per row, `seriesCount` = numeric-column count) naturally fail equality and remain untouched;
    #   · Duplicate series-identity logic: `str()` here versus normalization's `_string_cell()`
    #     (`None` / `"None"` / `2020.0` vs `2020`). Do not act when their counts disagree.
    # If two dimensions both match, do not guess between them. `_MAX_DRAWN_SERIES` still handles undrawable charts.
    matched = [k for k, v in per_dim.items() if len(v) == drawn]
    if len(matched) != 1:
        return spec, shape
    dim_key = matched[0]
    totals = per_dim[dim_key]
    # Sort totals descending with **series name as the second key**. Without it, equal totals (all zero/equal)
    # leave the retained 15 dependent on dict iteration order and change after restart (see the top-N tie lesson).
    ranked = sorted(totals, key=lambda k: (-totals[k], k))
    keep = set(ranked[:_MAX_TIME_SERIES])
    # Enforce the contract to populate this **only after actual truncation** here, without relying on incidental arithmetic in earlier gates:
    # marking unchanged rows makes all three consumers falsely claim 15 total series / first 15 displayed.
    if len(keep) >= len(totals):
        return spec, shape
    spec.rows = [r for r in rows if str(r.get(dim_key)) in keep]
    spec.series_shown = len(keep)
    spec.series_total = len(totals)
    # ★ Leave **the ranking itself** for callers to relay to the model.
    #
    # 116 dogfood 2026-09-24 `t_f4edf5b877b3`: this gate correctly selected the top 15 by total
    # (`女装` led at 6.249 billion), and the chart was correct, but the model received only the 15/38 count.
    # Its only category-named data was the **first ten inline rows** from `bi_query`: January's first ten categories
    # ordered by pinyin, led by `内暖衣` (49.88 million) and `厨房用具` (7.73 million).
    # The final answer therefore stated `『内暖衣』与『厨房用具』是绝对的销售支柱`, while the chart on the same screen
    # showed `女装` an order of magnitude higher, with no mention of it in the prose.
    #
    # Ranking is a **byproduct** of this gate with no extra computation. Do not add a `ChartSpec` field:
    # `spec_to_dict` serializes only dataclass fields. An ordinary attribute keeps persisted payloads and frontend output byte-identical.
    spec._series_rank = [(k, totals[k]) for k in ranked[:_MAX_TIME_SERIES]]  # type: ignore[attr-defined]
    kept_note = f"画出来 {drawn} 条系列 > {_MAX_TIME_SERIES}（读不清）→ 按合计取前 {len(keep)}/{len(totals)}"
    if spec.kind == "area_stacked":
        spec.kind = "line"
        kept_note += "，截断后堆叠的高度不再是真合计 → line"
    spec._infer_reason = f"{spec._infer_reason} → {kept_note}"
    return spec, None


def _undrawable_reason(spec: ChartSpec, shape: dict[str, Any] | None = None) -> str | None:
    """这张图画出来导出冻结得下吗——冻结不下（或根本读不了）时返回原因，否则 None。

    系列数与结构字节**直接问导出冻结样式用的那一份正规化**（`normalize_chart_for_rendering_v1`：
    长表摊开、时间宽表转置，与前端 `normalizeChartForRenderingV1` 对拍），不在这里另写
    「谁当 x 轴、谁摊成系列」。第一版自己写了一份：总把「非日期那一列」当系列——而柱状族是按
    **列序**取 x 轴的，`(门店, 日期, 金额)` 的柱状图于是被数成 5 条，实际摊开是 300 条（每天一条），
    导出照样整份失败。热力图 / 散点 / 桑基这些不摊开的图种，正规化后系列数自然就小，也不会误伤。

    唯一先于正规化的判断收在 `_undrawable_before_normalizing`（堆叠柱格数、热力图纵轴）：
    那一步本身就是开销所在，**任何**要跑正规化的调用方都得先过它。
    """
    cheap = _undrawable_before_normalizing(spec)
    if cheap is not None:
        return cheap

    if shape is None:
        from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1

        _, shape = normalize_chart_for_rendering_v1(spec_to_dict(spec))
    n_series = int(shape.get("seriesCount") or 0)
    if n_series > _MAX_DRAWN_SERIES:
        return f"画出来 {n_series} 条系列 > {_MAX_DRAWN_SERIES}"
    if spec.kind in _TIME_AXIS_KINDS:
        # Actual horizontal-axis slot count, **not** the time-point count in the data: long tables have one slot per time only when pivotable
        # (one numeric column + two dimensions + sufficient fill); otherwise each row gets a slot.
        # Measurement: 12 months × 100 brands × **two measures** cannot pivot, producing 1200 slots with each month repeated 100 times.
        slots = int(shape.get("categoryCount") or 0)
        # Allow 3000 for downsample-capable line charts; retain 1000 for bar charts. See `_MAX_BAR_SLOTS`.
        slots_cap = _MAX_LINE_POINTS if spec.kind in _DOWNSAMPLE_KINDS else _MAX_BAR_SLOTS
        if slots > slots_cap:
            return f"横轴上画出来 {slots} 个点 > {slots_cap}（糊成一片，该先聚合）"
    shape_bytes = len(json.dumps(shape, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    if shape_bytes > _MAX_DRAWN_SHAPE_BYTES:
        return f"结构 {shape_bytes}B > {_MAX_DRAWN_SHAPE_BYTES}B"
    return None


def _sink_constant_dims(columns: list[ChartColumn], const_keys: set[str]) -> list[ChartColumn]:
    """常量维度列沉到维度块末尾——让「列序第一个维度」= 「能区分行的那个」。

    ## 为什么这是必需的

    前端**每一个** builder 都按 `spec.columns.find(date|categorical|geo)` 取 x 轴，
    也就是**列序里第一个维度列**（`build-config.ts` 的 bar/line/dual_axis/combo/
    pie/waterfall… 共 8 处）。而取数侧常把 filter 维度（整列常量）排在分组维度
    **前面**：

        产品名称   医疗机构      销售额
        爱必妥     协和医院      1820
        爱必妥     华山医院      1655
        …

    于是 20 根横条**全叫「爱必妥」**，图什么也没说。116 语料实测 **9/2121**：
    `示例药品甲 Top 20 医院`、`示例药品乙 各省份销售额`（常量的是 `产品名称`）、
    `2025年各省月度刷卡金额走势`（常量的是 **date** 列 `每月`）。

    ## 与已有两处的关系（一个概念，三条路都要认）

    - `_lead_dim_col`（`canvas_tools`）判的是同一件事——**挑能区分行的，不是排第几**
      ——但它只在「宽到必须裁列」时才跑（`_needs_col_pruning`：>2 维或超序列上限）。
      这类「2 维 + 1 指标」的窄表根本触发不到。
    - 判定树规则 0a 把常量维度剔出**路由**，但发出去的 `columns` 原样不动。
      选型对了、画出来还是错的。

    所以路由与渲染读**同一份** `_constant_dim_keys`，21 条 `_make_spec` 分支
    和 `force_kind` 一起兜住。

    ## 只动维度列之间的相对次序

    数值列原地不动——序列顺序就是调色板顺序，动了会把线的颜色换掉。

    ⚠ 基数按**全量** `rows` 算（`_constant_dim_keys` 在 `infer_chart_spec` 入口
    算一次），不是内联那 50 行、也不是 top-N 截断后的那 30 行：表格「展开全部」
    后 FE 用的还是这份 `columns`，两边必须同源。
    """
    if not const_keys:
        return columns
    slots = [i for i, c in enumerate(columns) if c.type in _DIM_TYPES]
    if len(slots) < 2:
        return columns
    dims = [columns[i] for i in slots]
    ordered = sorted(dims, key=lambda c: c.key in const_keys)  # Stable: preserve relative order among distinguishable rows.
    if ordered == dims:
        return columns
    out = list(columns)
    for slot, col in zip(slots, ordered, strict=True):
        out[slot] = col
    return out


def _infer_from_rows(
    rows: list[dict[str, Any]],
    columns: list[ChartColumn],
    *,
    norm_hint: str,
    title: str,
    subtitle: str | None,
    data_artifact_id: str | None,
    force_kind: str | None,
    inline_rows_limit: int,
    const_dims: set[str],
) -> ChartSpec:
    """判定树本体。`rows` 已由调用方压到可展示的行数，`hint` 已归一。

    `const_dims` 由调用方按**全量** rows 算好（见 `_constant_dim_keys`）——不在这里
    重算，因为这里的 `rows` 可能已被 top-N 截断，截断后「碰巧同值」的列会比全量多。
    """
    n = len(rows)
    row_count = n

    # Always decide whether rows are inline. Prod 0.40.5 bug: a 98-row user table displayed `0-0/98 行`.
    # Old logic: above limit -> inline_rows=None -> _drop_empty removed spec.rows, leaving frontend
    # chart-table.tsx with no rows. Fix: still inline the first N above the limit (frontend renders
    # those rows plus show more / download CSV); the artifact retains all data for CSV download.
    inline_rows = rows if n <= inline_rows_limit else rows[:inline_rows_limit]

    # Force override
    if force_kind:
        coerced = _coerce_kind(force_kind)
        # Unit-family gate: `force_kind` must not put two unit groups on **the same value axis**.
        #
        # The intent layer already has this gate (`_kind_from_intent` rank branch emits hbar only with `n_axes == 1`),
        # as does the shape tree (rule 10: one cat + ≥2 num + two groups -> dual_axis). Only this path was missing:
        # explicitly naming the kind let the model bypass a guard that would otherwise reject it.
        #
        # Confirmed in 116 dogfood (2026-09-09 `t_e95476991dcb`):
        # `series=["Q3销售额_亿元","QoQ环比变化率_pct"]` + `force_kind="hbar"`,
        # two columns on one axis -> negative percentages expanded it to −40…+20, flattening all five sales bars at zero,
        # while the title was `Top 5 门店销售额**排名**`: the ranking became the least readable part.
        # Without `force_kind`, rule 10 gives the same data dual_axis with correct axis assignment.
        #
        # ⚠ **Do not duplicate criteria here**: ask the shape tree what it would choose without forcing, and override only when it answers
        # `dual_axis`. Unit-family grouping, ≥3-family tradeoffs and `combo` fallback then share
        # one implementation (`_axis_groups` / `_resolve_dual_axis`), keeping forced and automatic paths equivalent
        # rather than adding parallel criteria ([[feedback_one_concept_many_implementations]]).
        #
        # The `_upgrade_single_axis` safety net cannot catch this: it requires scale span ≥ `_FLAT_SPAN`
        # (100x), but this chart spans only 16.3x. Negative values expanding the axis cause the problem, not scale disparity.
        #
        # ⚠⚠ **hbar is a conditional exception.** `test_hbar_is_never_upgraded` fixes the contract
        # that hbar cannot upgrade to dual axis: frontend `buildDualAxis` lacks horizontal mode, so switching to vertical loses
        # the reason for horizontal bars: long category names. That contract holds for its own shape:
        # 12 rows named `很长的品牌名称-N`, where horizontal bars are the only readable arrangement.
        # The 116 chart instead has five rows such as `东湖街 / 西岸二期`, with **no valid reason for horizontal bars**;
        # the model merely habitually forces hbar for rankings. Override only when
        # hbar lacks its own structural reason, reusing rule 11's criterion
        # (`_long_labels_warrant_hbar`) rather than writing another implementation.
        #
        # When both reasons apply (long categories + two unit groups), preserve hbar's existing priority. The actual solution is
        # horizontal mode in `buildDualAxis`, a frontend change outside this scope.
        honors_own_reason = coerced == "hbar" and _long_labels_warrant_hbar(
            rows, [c for c in columns if c.type == "categorical" and c.key not in const_dims]
        )
        # With ≥3 unit families, **even hbar's rationale fails**: two axes cannot hold three unit types, much less one
        # (the shape tree has always returned table here; see rule 10). The `honors_own_reason`
        # exception is for two groups + long category names, where at least half of hbar remains readable.
        #
        # 116 measurements (1.3.51, 831 force_kind charts since 09-01): **ten** forced charts had shapes the tree deemed undrawable;
        # six were this case, typically
        # `各省营业额与利润同比增速`: revenue/year-over-year/profit/profit-year-over-year, four columns from three families on one axis.
        _forced_nums = [c for c in columns if c.type == "numeric"]
        _too_many_families = len(_axis_groups(rows, _forced_nums)) > 2 if len(_forced_nums) >= 2 else False
        # No dimension columns -> no category-axis names can render.
        #
        # 116 database measurements found **ten** such charts, all from `force_kind`, including three generated today.
        # Most striking: `2026年1-6月品类大类营业额排名 Top12` with `force_kind="hbar"`,
        # but **one row and one column** (only a 9.12-billion total) -> a single unlabeled horizontal bar,
        # despite the title promising a Top12 ranking.
        # The shape tree has an honest answer for **every one** of these ten: `kpi` for one row (number with its name),
        # otherwise `table`. This is not downgrading; return data that cannot support the requested chart.
        _no_dimension = not [c for c in columns if c.type in _DIM_TYPES]
        if coerced in _LABEL_NEEDING_KINDS and _no_dimension:
            auto = _infer_from_rows(
                rows,
                columns,
                norm_hint=norm_hint,
                title=title,
                subtitle=subtitle,
                data_artifact_id=data_artifact_id,
                force_kind=None,
                inline_rows_limit=inline_rows_limit,
                const_dims=const_dims,
            )
            auto._infer_reason = f"force_kind={force_kind} 但没有维度列，类目轴渲不出名字 → 交回形状树：{auto._infer_reason}"
            return auto
        if coerced in _SHARED_VALUE_AXIS_KINDS and (_too_many_families or not honors_own_reason):
            auto = _infer_from_rows(
                rows,
                columns,
                norm_hint=norm_hint,
                title=title,
                subtitle=subtitle,
                data_artifact_id=data_artifact_id,
                force_kind=None,
                inline_rows_limit=inline_rows_limit,
                const_dims=const_dims,
            )
            # ⚠ **Perform the axis upgrade before asking**. `_upgrade_single_axis` runs in outer
            # `infer_chart_spec`, while this gate is inside `_infer_from_rows`. Inspecting
            # `auto.kind` before upgrading misses shapes that become dual_axis only through an upgrade
            # (time axes: rule 4 first emits line, then sufficient span triggers upgrade),
            # leaving the gate permanently inactive for them.
            #
            # 116 measurement (deployed 1.3.47): `2026年月度营业额及同比趋势`,
            # revenue 9.15e8 + year-over-year rate 0.267, spanning 3.4 billion times. Without force_kind,
            # the outer layer returns correctly assigned dual_axis; with `force_kind="combo"` it returns to one axis,
            # flattening the percentage near zero on the 900-million-scale axis.
            # The inner kind is `line` (upgrade has not run), so `auto.kind == "dual_axis"` is false and the override passes.
            #
            # The extra upgrade is idempotent: upgraded kind `dual_axis` is outside `_UPGRADABLE_TO_DUAL`,
            # so the outer call returns it unchanged. Criteria still have one implementation ([[feedback_one_concept_many_implementations]]).
            auto = _upgrade_single_axis(auto, rows)
            if auto.kind == "dual_axis":
                auto._infer_reason = f"force_kind={force_kind} 会把 2 量纲组按在同一根值轴上 → 交回形状树：{auto._infer_reason}"
                return auto
            if _too_many_families:
                # The shape tree usually returns table for ≥3 families. This is not downgrading: these data have
                # **no** correct shared-axis representation; forcing one inevitably flattens at least one family near zero.
                auto._infer_reason = (
                    f"force_kind={force_kind} 会把 {len(_axis_groups(rows, _forced_nums))} 个量纲组按在共用值轴上 → 交回形状树：{auto._infer_reason}"
                )
                return auto
        return _make_spec(
            kind=coerced,
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=f"force_kind={force_kind}",
        )

    # 0a. Degenerate constant columns (dimension cardinality=1) are filter traces, not true dimensions. Exclude them from routing
    # so actual dimensions drive inference. Compute the criterion at `infer_chart_spec` entry (`_constant_dim_keys`),
    # sharing **the same result** with `_sink_constant_dims` column ordering; otherwise routing selects by A
    # while the frontend chooses its x axis by B, with each side only partly correct.
    #
    # ⚠ This previously recognized only `categorical`, so constant **date** columns still made charts time series:
    # `2025年各省月度刷卡金额走势` had one month and 50 provinces but produced a time-detected line,
    # all 50 x-axis points labeled `每月`. Four corpus charts had constant dates; all became line/area_stacked.
    cat_cols = [c for c in columns if c.type == "categorical" and c.key not in const_dims]
    num_cols = [c for c in columns if c.type == "numeric"]
    date_cols = [c for c in columns if c.type == "date" and c.key not in const_dims]
    geo_cols = [c for c in columns if c.type == "geo" and c.key not in const_dims]

    # 0. Empty data -> table displaying `无数据`.
    if n == 0:
        return _make_spec(
            kind="table",
            title=title,
            subtitle=subtitle or "无数据",
            rows=[],
            row_count=0,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason="n=0 → table (empty)",
        )

    # 0b. Rows exist but **no usable numeric values** -> table.
    #
    # Having rows does not imply having values: all-None / NaN columns render zero-height bars, concealing
    # whether data are missing or genuinely zero. Tables at least expose empty cells.
    #
    # ⚠ This accompanies batch 3 item ⑤: the earlier `n > 30 -> table` happened to catch this shape, but truncation
    # reduces it to 30 rows and then plots it normally. 116 acceptance-matrix R38/R39 found this
    # regression from my change, **not a production defect** (zero of 2024 real charts had entirely empty primary numeric columns);
    # restore the guard.
    if num_cols and not any(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v == v  # v==v excludes NaN.
        for c in num_cols
        for v in (r.get(c.key) for r in rows)
    ):
        return _make_spec(
            kind="table",
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason="数值列无可用值 → table",
        )

    # 1. One row with multiple purely numeric columns -> KPI card group.
    if n == 1 and not cat_cols and not date_cols and not geo_cols and num_cols:
        return _make_spec(
            kind="kpi",
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=f"n=1 + {len(num_cols)} numeric cols → kpi",
        )

    # 2. Whether multiple measures are drawable depends on **unit-family count + series count**, not column count.
    #
    # The old criterion `len(num_cols) >= 4 -> table` counted columns without asking whether they share a scale.
    # It rejected 15 charts on the 116 device, including **six with all measures in one unit family**
    # (`2025年月度核心指标趋势`: 12 rows × four currency measures), which should form a four-line chart.
    #
    # Replace it with two justified gates (a third marker-count gate narrows row caps by series count in `_display_row_cap`):
    if len(num_cols) >= 2:
        n_axes = len(_axis_groups(rows, num_cols))
        if n_axes > 2:
            # Same criterion as rule 10: two axes cannot hold three unit families; forcing them in misrepresents the data.
            return _make_spec(
                kind="table",
                title=title,
                subtitle=subtitle,
                rows=inline_rows,
                row_count=row_count,
                columns=columns,
                data_artifact_id=data_artifact_id,
                reason=f"{len(num_cols)} num → {n_axes} 个量纲族 > 2 → table",
            )
        # Tier series caps by unit family: same-family series are N slices of one measure, so more series merely add density;
        # different families are N distinct measures, whose smaller series flatten near zero on one axis.
        #
        # ⚠ **Do not mention the palette in reason**: it stopped being a constraint after `seriesColor()`,
        # and the model reads that reason and avoids add_chart accordingly (measured in 1.2.28). State the actual reason.
        cap = _draw_series_cap(rows, num_cols)
        if len(num_cols) > cap:
            same = "同族" if n_axes == 1 else f"{n_axes} 个量纲族"
            return _make_spec(
                kind="table",
                title=title,
                subtitle=subtitle,
                rows=inline_rows,
                row_count=row_count,
                columns=columns,
                data_artifact_id=data_artifact_id,
                reason=f"series={len(num_cols)}（{same}）> {cap} 条读不动 → table",
            )

    # 2.5 Intent layer: honor what the model explicitly requests (shape guards inside `_kind_from_intent`
    # return None to the shape tree below when undrawable). Deliberately place it:
    #   · **After** rules 0/1/2: empty data, single-row KPI and wide tables cannot support charts,
    #     so intent must not bypass them (canvas_tools trims wide-table columns upstream; do not relax this here).
    #   · **Before** rule 3 geo: that branch previously consumed every chart containing geo columns (12.7% in the live window),
    #     making province GMV rankings always vertical bars. Earlier intent routing allows hbar.
    intent = _kind_from_intent(norm_hint, rows, cat_cols, num_cols, date_cols, geo_cols, n)
    if intent is not None:
        intent_kind, intent_reason = intent
        return _make_spec(
            kind=intent_kind,
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=intent_reason,
        )

    # 3. Geographic dimension -> geo (Phase 2; currently bar fallback for Phase 1).
    #
    # Relax `len(num_cols) == 1` to `>= 1`: geo is outside `cat_cols`, so
    # geo + two numeric series matched neither rule 3 (exactly one column) nor rule 11 (cat_cols only),
    # falling through to rule 12's table. Province July-versus-June comparisons have this shape and should be
    # grouped bars. Rule 11 already allows 1..`_draw_series_cap` numeric series for cat-axis bar charts;
    # align geo axes with it. Rule 2's `num_cols >= 4 -> table` already intercepts earlier.
    if geo_cols and 1 <= len(num_cols) <= _draw_series_cap(rows, num_cols) and not date_cols:
        # Enable geo in Phase 2; use bars during MVP.
        return _make_spec(
            kind="bar",
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason="geo col detected; Phase 1 退化为 bar (Phase 2 换 geo)",
        )

    # 4. Time dimension -> line / stacked area.
    if date_cols and _time_points(rows, date_cols) >= 2:
        # Category groups & combination count >15 -> stacked area; otherwise single-/multi-series lines.
        if cat_cols:
            uniq_cat = len({r.get(cat_cols[0].key) for r in rows})
            if uniq_cat > 15:
                return _make_spec(
                    kind="area_stacked",
                    title=title,
                    subtitle=subtitle,
                    rows=inline_rows,
                    row_count=row_count,
                    columns=columns,
                    data_artifact_id=data_artifact_id,
                    reason=f"time + cat unique={uniq_cat} > 15 → area_stacked",
                )
        return _make_spec(
            kind="line",
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=f"time detected, n={n} → line",
        )

    # 5b/5c/6/7 (bridge / flow / share / correlation) moved into `_kind_from_intent`;
    # hint criteria exist only there. They were copied unchanged on moving, then `cat_cols` was unified to
    # `dims` (116 dogfood: `各省营业额占比` became bars instead of pie because `省` is geo).

    # 8. Parent/child / multiple categories / high cardinality -> stacked / heatmap.
    # 8a. Two cat + any num supports stacked_bar (cat1=x axis, cat2=series grouping,
    # at least one num; with multiple num, use only sort_by as the primary series and leave others for frontend switching).
    # Better than table fallback: asking for an X-by-Y breakdown implies grouped comparison rather than detail rows.
    if len(cat_cols) == 2 and len(num_cols) >= 1:
        if _order_parent_child(rows, cat_cols[0], cat_cols[1]) is not None:
            return _make_spec(
                kind="stacked_bar",
                title=title,
                subtitle=subtitle,
                rows=inline_rows,
                row_count=row_count,
                columns=columns,
                data_artifact_id=data_artifact_id,
                reason=f"2 cat + {len(num_cols)} num + parent-child → stacked_bar",
            )
        max_card = max(len({r.get(c.key) for r in rows}) for c in cat_cols)
        if max_card >= 20:
            return _make_spec(
                kind="heatmap",
                title=title,
                subtitle=subtitle,
                rows=inline_rows,
                row_count=row_count,
                columns=columns,
                data_artifact_id=data_artifact_id,
                reason=f"2 cat + {len(num_cols)} num + max_card={max_card} ≥ 20 → heatmap",
            )
        return _make_spec(
            kind="stacked_bar",
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=f"2 cat + {len(num_cols)} num → stacked_bar (multi-dim grouping)",
        )

    # 9. Multiple cat with no num, or ≥3 cat -> table.
    if len(cat_cols) >= 3 or (cat_cols and not num_cols):
        return _make_spec(
            kind="table",
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=f"cat={len(cat_cols)}, num={len(num_cols)} → table",
        )

    # 10. One cat + multiple num split into exactly two unit groups -> dual axis.
    #
    # Replace ≥10x scale disparity with `_axis_groups` grouping results (see that function's two root causes).
    # Group count determines three outcomes:
    #   One -> same axis; splitting same-family columns makes one billion and 100 million appear equally high -> rule 11.
    #   Two -> dual axis, one per group, faithfully comparing heights within each group.
    #   ≥3 -> **No dual_axis**: two axes cannot hold three unit types; forcing them in misrepresents data. Return to the shape tree
    #          (usually table). The actual solution needs a chart kind supporting multiple measures as a separate task.
    if len(cat_cols) == 1 and len(num_cols) >= 2:
        groups = _axis_groups(rows, num_cols)
        if len(groups) == 2:
            # `_make_spec` centralizes axis assignment for every dual_axis producer.
            return _make_spec(
                kind="dual_axis",
                title=title,
                subtitle=subtitle,
                rows=inline_rows,
                row_count=row_count,
                columns=columns,
                data_artifact_id=data_artifact_id,
                reason=(f"1 cat + {len(num_cols)} num → 2 量纲组 ({_axis_family(groups[0][0])} | {_axis_family(groups[1][0])}) → dual_axis"),
            )

    # 11. Standard bar (long category names -> hbar).
    if len(cat_cols) == 1 and 1 <= len(num_cols) <= _draw_series_cap(rows, num_cols):
        max_name_len = max(len(str(r.get(cat_cols[0].key, ""))) for r in rows)
        if _long_labels_warrant_hbar(rows, cat_cols):
            return _make_spec(
                kind="hbar",
                title=title,
                subtitle=subtitle,
                rows=inline_rows,
                row_count=row_count,
                columns=columns,
                data_artifact_id=data_artifact_id,
                reason=f"long labels (max_len={max_name_len}) + n={n} → hbar",
            )
        return _make_spec(
            kind="bar",
            title=title,
            subtitle=subtitle,
            rows=inline_rows,
            row_count=row_count,
            columns=columns,
            data_artifact_id=data_artifact_id,
            reason=f"1 cat + {len(num_cols)} num, n={n} → bar",
        )

    # 12. Table fallback.
    return _make_spec(
        kind="table",
        title=title,
        subtitle=subtitle,
        rows=inline_rows,
        row_count=row_count,
        columns=columns,
        data_artifact_id=data_artifact_id,
        reason=f"unmatched shape: cat={len(cat_cols)} num={len(num_cols)} date={len(date_cols)} n={n} → table",
    )


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────


def _make_spec(
    *,
    kind: ChartKind,
    title: str,
    subtitle: str | None,
    rows: list[dict[str, Any]] | None,
    row_count: int,
    columns: list[ChartColumn],
    data_artifact_id: str | None,
    reason: str,
) -> ChartSpec:
    # Dual-axis assignment belongs on the **mandatory path for every producer**, not inside one branch.
    # Previously only rule 10 assigned `y_axis_id`, leaving two other dual_axis producers without
    # assignments: intent-layer `hint=compare` (new in this batch) and model `force_kind="dual_axis"`.
    # Receiving all zeros sends the frontend to its defensive first-column-bar/rest-lines legacy path, assigning axes by column order
    # rather than units. Centralizing here automatically covers new producers.
    if kind == "dual_axis":
        kind, columns, reason = _resolve_dual_axis(rows or [], columns, reason)
    return ChartSpec(
        kind=kind,
        title=title,
        subtitle=subtitle,
        rows=rows,
        row_count=row_count,
        columns=columns,
        data_artifact_id=data_artifact_id,
        _infer_reason=reason,
    )


def _sparse_heatmap_reason(rows: list[dict[str, Any]], columns: list[ChartColumn]) -> str | None:
    """Convert only a categorical hierarchy that gives each item one populated cell to a stacked bar.

    One dimension must determine the other, so each bar has exactly one segment and no values are summed.
    Sparse crossed dimensions remain a heatmap because stacking could incorrectly add rates. Both selected
    dimensions must be categorical or geo: the stacked-bar path does not support a date dimension.
    The dimension selection matches frontend pickAxes and applies to every producer, including force_kind.
    """
    dims = [c for c in columns if c.type in ("categorical", "date")]
    if len(dims) < 2 or not rows:
        return None
    if any(c.type not in ("categorical", "geo") for c in dims[:2]):
        return None
    x, y = dims[0].key, dims[1].key
    cells = {(str(r.get(x)), str(r.get(y))) for r in rows}
    xs = {cell[0] for cell in cells}
    ys = {cell[1] for cell in cells}
    functional = len(cells) in (len(xs), len(ys)) and min(len(xs), len(ys)) > 1
    if not functional:
        return None
    return f"heatmap fills {len(cells)}/{len(xs) * len(ys)} cells (one cell per row) — a list, not a cross table → stacked_bar"


def _resolve_stack_dims(
    rows: list[dict[str, Any]],
    columns: list[ChartColumn],
) -> tuple[ChartKind, list[ChartColumn], str | None]:
    """Persist the category-axis and series decision for stacked bars.

    For a hierarchy, the child is the category axis and the parent supplies the series color. Each item then has
    one segment, so rates are not summed across children. Declaring roles prevents the frontend from making a
    conflicting decision based on column order. If both dimensions identify each other one-to-one, return a
    simple bar with the first dimension as its category. Genuinely crossed dimensions retain their column order.
    """

    dims = [c for c in columns if c.type in ("categorical", "geo")]
    distinct = {c.key: {str(r.get(c.key)) for r in rows} for c in dims}
    live = [c for c in dims if len(distinct[c.key]) > 1]
    if len(live) != 2:
        return "stacked_bar", columns, None
    a, b = live
    pairs = {(str(r.get(a.key)), str(r.get(b.key))) for r in rows}
    if len(pairs) == len(distinct[a.key]) == len(distinct[b.key]) == len(rows):
        return "bar", columns, f"{a.header} and {b.header} are one-to-one (the second only labels the first), stacking is meaningless → bar"
    order = _order_parent_child(rows, a, b)
    if order is None:
        return "stacked_bar", columns, None
    parent, child = order
    roles = {child.key: "x", parent.key: "color"}
    patched = [dataclasses.replace(c, role=roles[c.key]) if c.key in roles else c for c in columns]
    return "stacked_bar", patched, f"x={child.header} (child), series={parent.header} (parent)"


def _resolve_dual_axis(
    rows: list[dict[str, Any]],
    columns: list[ChartColumn],
    reason: str,
) -> tuple[ChartKind, list[ChartColumn], str]:
    """把 `dual_axis` 落到实处：派左右轴，或在只有一个量纲组时改出 `combo`。

    **单一量纲组不许出双轴。** 这条路是给 `force_kind="dual_axis"` 走的——模型点名
    要双轴，但数据其实同单位（真机：`2026年各月营业额与利润趋势`，两列都是百万元）。
    此时无论怎么派轴都会把同族列劈开，而那正是「两根柱画得一样高、读成持平」的谎。

    模型要的其实是**柱折混合**这个形态，而这个形态有专门的图种：`combo`（同轴柱折）。
    改出 combo 既满足了它的意图，又不用两根轴撒谎。这比「尊重 force_kind 但画错」
    和「无视 force_kind 退回柱状」都更接近它真正想要的东西。
    """
    num_cols = [c for c in columns if c.type == "numeric"]
    if len(num_cols) < 2:
        # Dual axis needs at least two numeric columns; otherwise leave it to the frontend table fallback (it raises and falls back itself).
        return "dual_axis", columns, reason
    groups = _axis_groups(rows, num_cols)
    if len(groups) < 2:
        return "combo", columns, f"{reason} → 单一量纲组，改 combo（同轴柱折）"
    right = {c.key for c in _right_axis_columns(rows, groups)}
    patched = [dataclasses.replace(c, y_axis_id=(1 if c.key in right else 0)) if c.type == "numeric" else c for c in columns]
    return "dual_axis", patched, reason


def _right_axis_columns(
    rows: list[dict[str, Any]],
    groups: list[list[ChartColumn]],
) -> list[ChartColumn]:
    """两根轴上放哪些列 —— 返回该去**右轴**的那些。

    两个族时没得选：大的左、小的右。

    **三个及以上族才是难点。** 原实现是 `groups[1:] 全去右轴`，那正是
    `_axis_groups` 的 docstring 早就写下、而这里没照做的那个反例：
    `退款额(1e5) / 退款量(1e3) / 退款率(0.03)` 三族，后两族一起塞右轴之后，
    退款率在一根 1e3 量级的轴上照样贴着零——分了等于没分。
    116 语料实测：90 张 dual_axis 里 **12 张是 ≥3 族，12 张右轴全都混了族**，
    最常见的形态就是「金额 + 数量 + 率」这种退款率/占比排名图。

    两根轴装不下三个族，这是硬约束；能选的只是**怎么并**。做法：
      1. 先定两个锚 —— 量级最大的族（左）和量级最小的族（右）。
         它们之间的距离就是「为什么需要第二根轴」本身。
      2. 其余族按 **log10 量级就近** 并入某一个锚。

    量级在这里可以用、而在 `_axis_groups` 里不许用，两者不矛盾：那边判的是
    「同族要不要劈开」（劈开就制造「子集与全集一样高」的谎），这边判的是
    「已经必须分开的族，往两根既定的轴上怎么靠」——不靠量级没有别的依据，
    而且靠错了的代价只是"挤了一点"，不是"读反了"。
    """
    if len(groups) <= 2:
        return [c for g in groups[1:] for c in g]

    mags = {c.key: (_column_magnitude(rows, c) or 0.0) for g in groups for c in g}

    def span(cols: list[ChartColumn]) -> float:
        """这根轴上最大量级 / 最小量级。跨度越大，小的那条越贴着零。"""
        vals = [mags[c.key] for c in cols if mags[c.key] > 0]
        return (max(vals) / min(vals)) if len(vals) > 1 else 1.0

    left, right = list(groups[0]), list(groups[-1])
    # Judge **which axis becomes worse after merging**, not distance to an anchor. The actual problem to avoid
    # is an axis span flattening the smaller series; use that directly as the objective.
    # Ties go left, reserving the right axis for the different unit family that necessitated a second axis.
    for group in groups[1:-1]:
        if max(span(left + group), span(right)) <= max(span(left), span(right + group)):
            left.extend(group)
        else:
            right.extend(group)
    return right


def _long_labels_warrant_hbar(
    rows: list[dict[str, Any]],
    cat_cols: list[ChartColumn],
) -> bool:
    """横条有没有**它自己的**结构性理由 —— 类目名长到竖着放不下。

    规则 11 与 `force_kind` 的量纲闸共用这一份判据。写成函数是因为两处要问同一个
    问题：量纲闸要判「夺走 `force_kind="hbar"` 会不会顺手把横条的理由也丢掉」，
    而那个理由就是规则 11 的触发条件本身。各写一份必然漂移。
    """
    if len(cat_cols) != 1 or not rows:
        return False
    max_name_len = max(len(str(r.get(cat_cols[0].key, ""))) for r in rows)
    return max_name_len > _LONG_LABEL_CHARS and len(rows) >= 10


def _coerce_kind(value: str) -> ChartKind:
    """宽容接受字符串 → ChartKind；无效走 table 兜底（不抛异常）."""
    from typing import get_args

    from bowhead_core.chart.types import ChartKind as _Kind

    allowed = set(get_args(_Kind))
    return value if value in allowed else "table"  # type: ignore[return-value]


def _order_parent_child(
    rows: list[dict[str, Any]],
    a: ChartColumn,
    b: ChartColumn,
) -> tuple[ChartColumn, ChartColumn] | None:
    """两个类别列构成父子层级吗？是就返回 `(parent, child)`，否则 None。

    ## 谁是 parent 按**基数**定，不按列序

    1-to-many 里 parent 的取值必然更少（省 → 市）。原实现直接按列序传
    `(cat_cols[0], cat_cols[1])`，而取数侧给回来的列序常常是反的：
    `门店(40) / 效能象限(4)`、`品牌(50) / quadrant(4)` —— 真正的层级是
    象限 → 门店。语料实测 44 张走这条规则的图里 **7 张**因此被判成「不是父子」，
    掉到高基数分支出了 **40×4 的热力图**，而标题（`门店坪效 vs 利润率四象限`、
    `关店建议：综合评分排名`）要的是按象限堆叠。

    ## 常量列不算 parent

    整列同值时，「每个 child 只属于一个 parent」**恒成立**——启发式反过来
    「确认」了一个错误的层级，x 轴会全是同一个值（`示例药品甲 Top 20 医院` 的
    `产品名称` 整列是「示例药品甲」）。那是**过滤维度**，不是层级的一层。
    与 `canvas_tools._lead_dim_col` 同源：常量维度列从来不该当类目轴。
    """
    card_a = len({str(r.get(a.key)) for r in rows})
    card_b = len({str(r.get(b.key)) for r in rows})
    # Lower cardinality determines the parent; ties preserve column order (existing behavior).
    parent, child = (a, b) if card_a <= card_b else (b, a)
    if min(card_a, card_b) <= 1:
        return None  # Constant columns are filter dimensions, not hierarchy.
    return (parent, child) if _is_parent_child(rows, parent, child) else None


def _is_parent_child(
    rows: list[dict[str, Any]],
    parent: ChartColumn,
    child: ChartColumn,
) -> bool:
    """判定两个类别维度是否存在父子关系.

    启发式：每个 child value 只在**一个** parent value 下出现（严格 1-to-many）.
    例：省 → 市，一个市只属于一个省.
    """
    child_to_parents: dict[Any, set[Any]] = {}
    for r in rows:
        p = r.get(parent.key)
        c = r.get(child.key)
        if p is None or c is None:
            continue
        child_to_parents.setdefault(c, set()).add(p)
    if not child_to_parents:
        return False
    return all(len(parents) == 1 for parents in child_to_parents.values())


def _column_magnitude(rows: list[dict[str, Any]], col: ChartColumn) -> float:
    """一列数值的量级（非零绝对值的均值）。空 / 全 None / 全 0 → 0.0。

    ⚠ 只用来给 `_axis_groups` 的输出**排序**（量级大的组排在前 → 左轴），
    **不参与分组判定**。分组只看量纲族——见 `_axis_groups` 的 docstring：
    同族按量级劈开正是「子集与全集画得一样高」那个谎的来源。
    """
    vals = [r.get(col.key) for r in rows]
    nums = [abs(float(v)) for v in vals if isinstance(v, (int, float)) and v != 0]
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def _axis_family(col: ChartColumn) -> str:
    """这一列的**量纲族** —— 同族才可能共用一根值轴。

    先信产出方在列上声明的（`format` / `unit` / `percent_scale`），declare 不到时
    退回 `units.infer_from_name` 按列名猜。**只用 `kind` 这一层**：一列是不是百分比
    决定它要不要单独一根轴，而它是 fraction 还是 points（0.18 还是 18）只影响
    **怎么格式化**，不影响**放哪根轴**——两件事分开，别把 percent_scale 的缺口
    传染到轴分配上（那个缺口需要产出方声明，短期补不齐）。
    """
    from bowhead_core import units

    if col.percent_scale or col.format == "percent":
        return "percent"
    if col.format == "currency":
        # Different currencies cannot share an axis: plotting ¥ and $ together invents a nonexistent exchange rate.
        return f"currency:{col.unit or ''}"
    spec = units.resolve(col.header or col.key, unit_name=col.unit or "")
    if spec is None:
        return f"plain:{col.unit or ''}"
    if spec.kind == "currency":
        return f"currency:{spec.code or ''}"
    if spec.kind == "duration":
        return f"duration:{spec.label or ''}"
    if spec.kind == "percent":
        return "percent"
    return f"{spec.kind}:{spec.label or col.unit or ''}"


def _axis_groups(
    rows: list[dict[str, Any]],
    num_cols: list[ChartColumn],
) -> list[list[ChartColumn]]:
    """把数值列分成若干「可以共用一根值轴」的组，按组内最大量级降序。

    **判据只有量纲族，不看量级。** 两条方向相反的理由各管一半：

    - **不同族必须分**：元 和 % 共用一根轴，其中一个必然被压成贴零的线。
    - **同族一律不分**：分了之后相对高低就失真。真机逮到的最刺眼一例是
      `各市会员客流 vs 总客流量`——两列同为人次、会员客流是总客流的**子集**，
      各占一根轴后画得一样高，读成「会员占了全部客流」，与事实相反。

    ⚠ 我第一版实现把量级也写进了分组条件（同族但差 ≥10x 也劈开），那正好制造了
    上面这个谎；「同族列必须同轴」是我自己写下的原则，实现却违反了它。
    同族里某一列确实小到看不见时，诚实的答案是「它就是小」，或者换成能承载多指标
    的图种（小倍数），不是给它一根自己的轴假装一样高。

    ⚠ 也不是原来那句「最大量级的放左轴、其余全放右轴」：那个策略在 ≥3 个指标时
    会把客流量(1e6) 和 利润率(0.15) 一起塞进右轴，利润率照样贴着零——分了等于没分。
    """
    groups: list[list[ChartColumn]] = []
    mags: dict[str, float] = {}
    by_family: dict[str, list[ChartColumn]] = {}
    for col in num_cols:
        mags[col.key] = _column_magnitude(rows, col)
        family = _axis_family(col)
        if family not in by_family:
            by_family[family] = []
            groups.append(by_family[family])
        by_family[family].append(col)
    groups.sort(key=lambda g: max((mags.get(c.key) or 0.0) for c in g), reverse=True)
    return groups
