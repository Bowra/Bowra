"""ChartSpec —— 前后端共识的图表契约.

前端 TS 镜像见 packages/bowhead_frontend/lib/chart/types.ts.
任何 schema 变更**必须双边同步**，否则 canvas hydrate 会挂.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from bowhead_core.chart.numeric_display import NumericDisplay

ChartKind = Literal[
    "kpi",  # KPI card group with multiple numeric columns in one row.
    "line",  # Line.
    "bar",  # Vertical bars.
    "hbar",  # Horizontal bars (for long category names).
    "pie",  # Pie / doughnut.
    "stacked_bar",  # Stacked bars.
    "dual_axis",  # Dual-axis bar/line combination (different units/scales; second series on the right axis).
    "combo",  # V2.9 B1: same-axis bar/line combination (Target bar + Actual line + Forecast line, same unit).
    "scatter",  # Scatter.
    "heatmap",  # Heatmap (chartjs-chart-matrix, enabled in WP2).
    "waterfall",  # Waterfall/bridge (financial bridge, hint=bridge; frontend floating bars).
    "sankey",  # Sankey flow (hint=flow; chartjs-chart-sankey).
    "area_stacked",  # Stacked area.
    "geo",  # Map (Phase 2, requires chartjs-chart-geo).
    "table",  # Fallback table.
]

ChartColumnType = Literal["numeric", "categorical", "date", "geo"]
ChartColumnRole = Literal["x", "y", "color", "size", "facet"]
ChartColumnFormat = Literal["int", "float", "percent", "currency", "datetime"]

ChartHint = Literal[
    "trend",
    "rank",
    "share",
    "compare",
    "correlation",
    "composition",
    "raw",
    "bridge",
    "flow",  # WP2: waterfall contribution decomposition / Sankey flow.
]
VALID_HINTS: frozenset[str] = frozenset(
    [
        "trend",
        "rank",
        "share",
        "compare",
        "correlation",
        "composition",
        "raw",
        "bridge",
        "flow",
    ]
)


@dataclass
class ChartColumn:
    """一列数据的元信息."""

    key: str  # Field name in rows.
    header: str  # Display name.
    type: ChartColumnType
    # x / y / color / size / facet; use positional order when omitted. Producer today: stacked bars over a two-level
    # hierarchy declare the child `x` and the parent `color` (`infer._resolve_stack_dims`); read by the web normalizer
    # (`roleOrderedDims` / `_role_ordered_dimensions`) and the Feishu renderer.
    role: ChartColumnRole | None = None
    format: ChartColumnFormat | None = None  # Numeric formatting.
    unit: str | None = None  # Stored quantity unit.
    currency_code: str | None = None
    display: NumericDisplay | None = None
    date_pattern: str | None = None
    # Percentage shape must come from source facts or a producer declaration.
    # Unknown shape is an ordinary number; names and ranges never establish it.
    percent_scale: str | None = None
    percent_shape_unknown: bool | None = None
    source_header: str | None = None
    # Sprint chat-v4-streaming Phase 4E — dual_axis per-column axis assignment.
    # 0 = left axis (default for non-dual), 1 = right axis. Only ChartSpec.kind
    # == "dual_axis" populates this; other kinds ignore the field. Backward
    # compat: older canvas artifacts without this field deserialize to 0 (the
    # `_drop_empty` filter drops 0/None on the wire, so absence == default).
    y_axis_id: int = 0


@dataclass
class ChartSpec:
    """完整的图表规格（前后端序列化通过）."""

    kind: ChartKind
    title: str
    subtitle: str | None = None

    # Data. Charts: `rows` contains all plotted rows (limit in `infer._CHART_ROWS_MAX`; `top_n` describes truncation).
    # Tables: `rows` contains only the first 50 rows; Expand all retrieves the rest from the file using `data_artifact_id`.
    rows: list[dict[str, Any]] | None = None
    data_artifact_id: str | None = None
    row_count: int = 0  # Actual total row count (may exceed rows length).

    columns: list[ChartColumn] = field(default_factory=list)

    # Display options (frontend uses its own defaults when None).
    limit_rows: int = 30
    top_n: int | None = None
    # Series truncation is distinct from `top_n` (only the first N **rows** plotted); do not combine the two:
    # truncating 59 provinces to 15 lines changes rows from 708 to 180; reporting this as top_n claims only 180 items displayed,
    # confusing series counts with rows. Both can occur simultaneously, so each needs a separate field.
    # Set only for **actual truncation**; `spec_to_dict` drops None, leaving existing payloads byte-identical.
    series_shown: int | None = None
    series_total: int | None = None
    # Time-axis downsampling (`_downsample_time_axis`) is **independent** of both counts above and `top_n`:
    # a chart may truncate series and downsample together; each needs different wording, and merging any two gives false claims.
    # Likewise, set only for **actual downsampling**.
    points_shown: int | None = None
    points_total: int | None = None
    sort_by: str | None = None
    secondary_axis: bool = False
    show_legend: bool = True
    # ⚠ Must be `None` (no declaration), never `False`.
    #
    # Frontend resolveDataLabels first prioritizes **explicit** booleans, then follows the ladder:
    # always enabled on exports -> always enabled on mobile -> enabled by default for one series with ≤N categories. Default False
    # is persisted unchanged by spec_to_dict (`_drop_empty` only drops None/[]/{}),
    # sending every chart to the frontend with explicit false; all 2781/2781 charts in 116 production were
    # false, with **no assignment anywhere** in Python, so the four lower ladder levels had never executed since launch.
    # Only an undeclared server value lets frontend policies by surface/density operate.
    show_data_labels: bool | None = None
    percent_stack: bool = False

    fallback_iframe_url: str | None = None
    fallback_kind: ChartKind = "table"

    # P3.6: source_ref metadata for the chart's top source row (tool / query / sql / artifact_id / turn_id / tool_call_id).
    # Same schema as narrative SourceRef, embedded in the chart without a separate source_refs lookup.
    source_meta: dict[str, Any] | None = None

    # Debug (the _ prefix tells the LLM it can ignore this; humans use it to inspect infer selection logic).
    _infer_reason: str | None = None

    # Sprint ι (PRD §4.C.2): True if backend's 5-AND-gate auto-emitted this
    # chart (LLM forgot add_chart). Frontend renders i18n subcaption
    # `chart.auto_from_sandbox` to surface uncertainty.
    auto_charted: bool = False

    # Optional composition owned by the existing native chart section. Every view
    # uses this section's dataset and source; child specs never carry source objects.
    views: list[dict[str, Any]] | None = None
    layout: dict[str, Any] | None = None
    derived_columns: list[dict[str, Any]] | None = None
    calculation_missing: dict[str, int] | None = None
    # Canonical host-compiled evidence; input refs and viewer projection remain separate.
    presentation_plan: dict[str, Any] | None = None


def capped_series(spec: Any) -> tuple[int, int] | None:
    """系列被截了吗——截了返回 `(画出几条, 一共几条)`，没截返回 None。

    三个消费方（网页图注 `chart-block.tsx`、飞书卡片 `feishu/renderer.py`、
    回给模型的 `add_chart` 结果）必须用**同一条**判据。此前三处各写各的：
    飞书不要求 `shown > 0`，于是 `series_shown=0` 会印出「按合计显示前 **0** 条」、
    `-1` 会印出「前 -1 条」，而同一份输入网页一句不出（复审实测）。
    后端今天写不出这些值，但「一个概念三处实现」本身就是缺陷。

    `spec` 可以是 `ChartSpec`，也可以是已经序列化的 dict（飞书拿到的是后者）。
    """
    return _cap_pair(spec, "series_shown", "series_total")


def _cap_pair(spec: Any, shown_key: str, total_key: str) -> tuple[int, int] | None:
    """`(画出几个, 一共几个)` 的共用校验：两个都得是真正的正整数，且**真的少画了**。

    `bool` 是 `int` 的子类，必须单独挡掉；`0` / 负数 / 字符串 / 浮点一律当没截——
    后端今天写不出这些值，但判据摆在这里就不会有第二份走样的实现
    （复审实测：飞书那份不要求 `shown > 0`，`series_shown=0` 会印出「显示前 **0** 条」）。
    """
    get = spec.get if isinstance(spec, Mapping) else lambda k: getattr(spec, k, None)
    shown, total = get(shown_key), get(total_key)
    if not isinstance(shown, int) or isinstance(shown, bool) or shown <= 0:
        return None
    if not isinstance(total, int) or isinstance(total, bool) or total <= shown:
        return None
    return shown, total


def downsampled_points(spec: Any) -> tuple[int, int] | None:
    """时间轴被降采样了吗——降了返回 `(画出几个时刻, 一共几个时刻)`，没降返回 None。

    与 `capped_series` 同一套形状校验，理由也一样：三个消费方必须用同一条判据，
    各写各的会让同一份 spec 三种说法。
    """
    return _cap_pair(spec, "points_shown", "points_total")


def spec_to_dict(spec: ChartSpec) -> dict[str, Any]:
    """序列化 ChartSpec 给 canvas_store.append_section 存 / SSE 广播.

    dataclasses.asdict 会递归 dict-ify 子 dataclass（ChartColumn）.
    drop `None` + 空 list 省 payload 体积；debug 字段保留便于排查.
    """
    raw = dataclasses.asdict(spec)
    return _drop_empty(raw)


def _drop_empty(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: v if k == "presentation_plan" else _drop_empty(v) for k, v in d.items() if v is not None and v not in ([], {})}
    if isinstance(d, list):
        return [_drop_empty(x) for x in d]
    return d
