"""Canvas-facing tools for LLM：`add_chart` / `canvas_add_external_iframe`.

`add_chart` 是 Phase 1 主入口：LLM 拿 fetch_rows 返的 artifact_id → 这里读 CSV →
infer_chart_spec → 落 canvas_store + 实时 SSE 广播. 前端即时 hydrate.

（`canvas_add_iframe` 已下线：datarc 卡片不再嵌 iframe，一律走原生 chart；datarc_iframe
section kind + FE renderer 仅保留渲染历史报告。外部 URL 嵌入走 `canvas_add_external_iframe`。）
"""

from __future__ import annotations

import contextlib
import copy
import csv as _csv
import io as _io
import json
import logging
import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, NamedTuple

from bowhead_router.types import ToolSchema

from bowhead_core.agents._lead_context import run_ref_allowlist
from bowhead_core.canvas_store import CanvasRevConflictError
from bowhead_core.chart import ChartColumn, infer_chart_spec, spec_to_dict
from bowhead_core.chart.expression import composition, derive_columns, schema_properties
from bowhead_core.chart.infer import _take_top_n, indistinguishable_rows_reason
from bowhead_core.chart.numeric_display import apply_column_display, currency_identity
from bowhead_core.chart.presentation_plan import ChartPlanError, compile_notes_plan, evidence_notes_schema, parse_plan_input, verify_plan_notes
from bowhead_core.chart.types import VALID_HINTS, capped_series, downsampled_points
from bowhead_core.chart.units_enrich import (
    can_enrich_from_current_datarc_turn,
    enrich_columns_with_units,
    lookup_from_source,
    mark_unknown_percent_shape,
    relabel_columns_for_locale,
)
from bowhead_core.design_flags import is_design_v2a_enabled
from bowhead_core.design_schema import (
    DESIGN_SIZE_PRESETS,
    clean_design_text,
    design_size_metric_label,
    design_text_layer_summary,
    normalize_design_payload,
)
from bowhead_core.events import OrchestratorEvent
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)


class _PreparedChartDataset(NamedTuple):
    """One authorized data snapshot shared only by a single composed chart call."""

    artifact: Any  # ToolExecutionContext injects the artifact-store protocol; test/deployment records are structurally compatible.
    rows: list[dict[str, Any]]
    columns: list[ChartColumn]
    non_finite: dict[str, int]
    reselect_meta: dict[str, Any] | None
    derived_bytes: bytes | None
    calculation_metadata: list[dict[str, Any]] | None
    calculation_missing: dict[str, int] | None
    derived_row_count: int
    raw_bytes: bytes


# 0.39.7 #3 — artifact_store.py:351 generates `art_{uuid4().hex[:12]}` so the
# canonical format is `art_` + 12 lowercase hex. LLM occasionally hallucinates
# `data_artifact_id="4"` (a row number) or other non-id strings; we silent-strip
# those instead of letting them bubble to `artifact_store.get` → "not found"
# tool failure → red chip in chat. Returns success-shaped result with explicit
# `skipped` reason so LLM can correct next iteration.
_VALID_DATA_ARTIFACT_ID_RE = re.compile(r"^art_[a-f0-9]{12}$")
_SIMPLE_INT_REF_RE = re.compile(r"^\d{1,4}$")
_SERIES_TITLE_HINTS = ("走势", "趋势", "月度", "各月", "每月", "同比", "环比", " vs ", "VS")
_TIME_COLUMN_HINTS = ("月份", "每月", "年月", "日期", "时间", "周", "季度", "year", "month", "date")
_MONTHLY_FILENAME_HINTS = ("各月", "月度", "每月", "逐月")
# #5 (dingsiyan 2026-07-07): titles implying a temporal PROGRESSION genuinely need
# ≥2 time points; a 1-row scalar under these is a wrong-artifact mistake. Narrower
# than _SERIES_TITLE_HINTS — excludes 同比/环比/vs, which can legitimately be 1 row.
_TREND_PROGRESSION_HINTS = ("走势", "趋势", "月度", "各月", "每月", "逐月")

_METRIC_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("refund_rate", ("退款率", "退货率", "refund")),
    ("profit_margin", ("利润率", "毛利率", "margin")),
    ("traffic", ("客流量", "客流", "人次", "traffic")),
    ("sales_volume", ("销售量", "销量", "件数", "volume")),
    ("floor_efficiency", ("月坪效", "坪效")),
    ("ipt", ("ipt", "pit", "客单价")),
    ("profit", ("利润", "profit")),
    ("revenue", ("营业额", "营收", "收入", "销售额", "revenue", "turnover")),
)
_METRIC_EXCLUSION_TERMS: dict[str, tuple[str, ...]] = {
    "revenue": ("利润", "利润率", "客流", "销售量", "销量", "退款率", "月坪效", "ipt", "pit"),
    "profit": ("营业额", "营收", "收入", "客流", "销售量", "销量", "退款率", "月坪效", "ipt", "pit"),
    "traffic": ("营业额", "营收", "收入", "利润", "销售量", "销量", "退款率", "月坪效", "ipt", "pit"),
    "refund_rate": ("营业额", "营收", "收入", "利润", "客流", "销售量", "销量", "月坪效", "ipt", "pit"),
    "profit_margin": ("营业额", "营收", "收入", "客流", "销售量", "销量", "退款率", "月坪效", "ipt", "pit"),
    "sales_volume": ("营业额", "营收", "收入", "利润", "客流", "退款率", "月坪效", "ipt", "pit"),
    "floor_efficiency": ("营业额", "营收", "收入", "利润", "客流", "销售量", "销量", "退款率", "ipt", "pit"),
    "ipt": ("利润", "利润率", "客流", "销售量", "销量", "退款率", "月坪效"),
}
_LATIN_TERM_CHARS = r"a-z0-9_"


def _contains_search_term(text: str, term: str) -> bool:
    term_l = str(term or "").lower()
    if not term_l:
        return False
    text_l = str(text or "").lower()
    if re.search(r"[a-z0-9]", term_l):
        return (
            re.search(
                rf"(?<![{_LATIN_TERM_CHARS}]){re.escape(term_l)}(?![{_LATIN_TERM_CHARS}])",
                text_l,
            )
            is not None
        )
    return term_l in text_l


def _segments_in_text(text: str) -> set[str]:
    text_s = str(text or "")
    segments: set[str] = set()
    without_non_member = text_s
    if "非会员" in without_non_member:
        segments.add("非会员")
        without_non_member = without_non_member.replace("非会员", "")
    if "会员" in without_non_member:
        segments.add("会员")
    return segments


def _metric_inc(name: str, labels: dict[str, str] | None = None) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(name, labels=labels or {})
    except Exception as exc:
        logger.debug("canvas_tools.metric_failed name=%s err=%s", name, exc)


def _design_size_metric_label(value: Any) -> str:
    return design_size_metric_label(value)


def register_canvas_tools(registry: ToolRegistry) -> None:
    """canvas.add_* tool 家族: chart / insight / iframe / html_artifact / external_iframe / image."""
    registry.register(ToolSpec(schema=_add_chart_schema(), handler=_add_chart))
    registry.register(ToolSpec(schema=_add_insight_schema(), handler=_add_insight))  # M157
    registry.register(
        ToolSpec(
            schema=_add_html_artifact_schema(),
            handler=_add_html_artifact,
        )
    )
    registry.register(
        ToolSpec(
            schema=_add_external_iframe_schema(),
            handler=_add_external_iframe,
        )
    )
    # multimodal-v1: image_generate output → Canvas image section (additive evidence)
    registry.register(ToolSpec(schema=_add_image_schema(), handler=_add_image))
    # multimodal-v2A: image background + editable text layers
    if is_design_v2a_enabled():
        registry.register(ToolSpec(schema=_create_design_schema(), handler=_create_design))
        registry.register(
            ToolSpec(
                schema=_update_design_text_schema(),
                handler=_update_design_text,
            )
        )
        registry.register(ToolSpec(schema=_flatten_design_schema(), handler=_flatten_design))
    else:
        logger.info("editable design tools not registered (BOWHEAD_DESIGN_V2A off)")
    # S3: structured spec_block diagrams (attribution chains/metric trees/flows; dagre+SVG, not DSL)
    registry.register(ToolSpec(schema=_add_spec_block_schema(), handler=_add_spec_block))
    # canvas-update-turn batch B2-2: follow-up revision softly folds previous-turn charts, expandable at report end. Not registered with flag off.
    from bowhead_core.canvas_revise import section_revise_enabled

    if section_revise_enabled():
        registry.register(ToolSpec(schema=_remove_chart_schema(), handler=_remove_chart))


# ─────────────────────────────────────────────────────────────
# add_chart
# ─────────────────────────────────────────────────────────────
def _add_chart_schema() -> ToolSchema:
    return ToolSchema(
        name="add_chart",
        description=(
            "把一份数据加到画布作为**原生交互图表**（折线/柱/饼/瀑布/桑基/热力 自动选型，"
            "用户可切换视角）. **画图默认走这里** — 数据源两种: "
            "①`bi_query` 返的 `_ref_artifact_id`; "
            "②**`python_sandbox` 算出的数据**（code 里 `df.to_csv('out.csv', index=False)`，"
            "取 `output_artifacts[0].artifact_id` 传进来 — 不要 `savefig` 出 PNG）. "
            "**不要**把 rows 逐字填进来；数据走 artifact_id 引用."
        ),
        input_schema={
            "type": "object",
            "required": ["data_artifact_id", "title"],
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["chat", "canvas"],
                    "description": (
                        "这张图去哪：`chat`=只进聊天气泡、不上画布；`canvas`=进画布。"
                        "**不确定就别传**——不传时沿用本轮的交付面判定，那是默认且正确的行为。"
                        "典型用途：报告轮里画了一张只为核对的中间图，传 `chat` 让它不污染报告。"
                        "⚠ 只能收窄：本轮已确定是聊天答复时，传 `canvas` 会被忽略。"
                    ),
                },
                "data_artifact_id": {
                    "type": "string",
                    "description": (
                        "`bi_query` 返的 `_ref_artifact_id` / `python_sandbox` 返的 "
                        "`output_artifacts[].artifact_id` (art_xxx), 或 "
                        "<available_refs> 里 kind=data 的 simple id. "
                        "不要填 chart_section_id / chart simple id."
                    ),
                },
                "hint": {
                    "type": "string",
                    "enum": ["trend", "rank", "share", "compare", "correlation", "composition", "raw", "bridge", "flow"],
                    "default": "raw",
                    "description": (
                        "Optional analysis intent: trend=time trend; rank=ranking; share=share; compare=comparison; "
                        "correlation=relationship; composition=stacked composition; bridge=signed contributions "
                        "to a total change (one category and one numeric increment column); flow=source-to-target "
                        "flow (source, target and flow amount columns); raw=prepared observations without a specific "
                        "analysis intent. Omitted hint defaults to raw. For an explicitly requested Table, use "
                        "force_kind=table. "
                        "The host selects a native chart from the hint and dataset shape; explicit force_kind is preserved."
                    ),
                },
                "series": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "（可选，宽表强烈建议）要画的**数值列名**，按顺序。维度轴（省/日期/分类）"
                        "服务端自动带上，这里只列数值列。不传时服务端按 hint 猜一列，"
                        "宽表上很容易猜错——例如 18 列的表里想画「7月 vs 6月」，"
                        "不传就只会画出 7 月一根柱子。想对比就写全：['营业额', '营业额_6月']. "
                        "⚠ **用户点名的指标列必须写进来**：数据里有、但没进 series 的列不会进图"
                        "（真机踩过：artifact 里有『营业额同比变化率』列，series 只写了两条绝对值，用户点名的那项就没了）。"
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "图表标题（≤ 30 字）",
                },
                "subtitle": {
                    "type": "string",
                    "description": "（可选）副标题",
                },
                "force_kind": {
                    "type": "string",
                    # chart-surfacing-fix WP3: match chart/types.py ChartKind; add combo /
                    # waterfall / sankey / heatmap (already implemented in FE chart-block.tsx SUPPORTED_CHARTJS).
                    # Deliberately omit geo: chartjs-chart-geo is not installed; requesting it falls back to ChartFallbackTable.
                    "enum": [
                        "kpi",
                        "line",
                        "bar",
                        "hbar",
                        "pie",
                        "stacked_bar",
                        "dual_axis",
                        "combo",
                        "scatter",
                        "area_stacked",
                        "waterfall",
                        "sankey",
                        "heatmap",
                        "table",
                    ],
                    "description": (
                        '（可选）强制图表类型。**用户点名了形态就填**（点名要表格 → `"table"`，'
                        "不看行数）；用户没点名就别填，让推断引擎按数据形状决定。"
                        "⚠ `scatter` **会自动给点标名字**：从分类列取身份，挑出互不重叠的一批"
                        "常驻标注（导出/触屏下必标），其余 hover 可见。"
                        "所以用户说「每个点标上名字」时用这个，**不要转去 savefig 出图片**。"
                    ),
                },
                "column_units": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 32},
                    "description": "Optional exact numeric CSV column keys to actual source units, for example revenue: CNY. Include a column only when the data source or the user states its unit, and give the unit itself (CNY, 万元, kg), not a description. If no unit is stated, omit the column (or omit column_units entirely); put any note about an unknown or unconfirmed unit in subtitle. Tables name units once in the column header; only percentage marks repeat after each value. These describe supplied values, not an axis display scale. Keep original prepared values; never divide or round merely for display. Native axes and labels choose readable magnitudes. If source values are already scaled, preserve that actual unit, for example CNY 10k. Never infer currency from a title. Percentage levels use % with percent_fraction or percent_points in column_types. Fraction/points describe storage. A percentage-point difference is a separate numeric measure with its stated unit.",
                },
                "column_display": {
                    "type": "object",
                    "description": "Optional precision by exact final numeric column key, including derived columns. Each entry has decimal_places (integer 0–12) and decimal_mode (max trims zeroes, fixed retains them). Omit for readable defaults. Unsupported entries fall back independently; never round source rows for display.",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "decimal_places": {"type": "integer", "minimum": 0, "maximum": 12},
                            "decimal_mode": {"type": "string", "enum": ["max", "fixed"]},
                        },
                        "required": ["decimal_places", "decimal_mode"],
                        "additionalProperties": False,
                    },
                },
                "column_types": {
                    "type": "object",
                    "description": (
                        "（可选）override CSV 列类型识别. 键=列名，值=numeric/"
                        "categorical/date/geo. 比如 '月份' 列默认可能被识别为 "
                        "categorical，想画时间趋势就 override 为 date.\n"
                        "★ **比率列请务必声明形状**：`percent_fraction`（0.83 表示 83%）"
                        "或 `percent_points`（13.5 表示 13.5%）。你在沙箱里算的比率"
                        "（`df['占比'] = x / total`）**只有你知道**除没除 100——"
                        "不声明的话系统只能按列名和值域猜，而 `占比 0.0416` 到底是 "
                        "4.16% 还是 0.04% 是猜不出来的（真实客户两种约定都存在）。"
                        "等价做法：CSV 里直接写成 `15.37%` 文本（= 已放大 ⇒ points）。\n"
                        "显式写 `numeric` 表示「普通数，**不加**百分号」——系统不会再按列名猜；"
                        "已经 ×100 又想显示成 `112.3%` 的列请用 `percent_points`。\n"
                        "⚠ **沙箱算出来的数据里，没声明形状的数值列一律按普通数显示（不加 %）**"
                        "——系统不替你猜；想显示百分号就必须声明。"
                        "原列和它的 ×100 列别声明成同一种形状（一个数和它的 100 倍不可能都是 fraction），"
                        "否则两列都会退回普通数。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "（可选）只画前 N 项，用于排名/Top 榜。按主数值列取真正的前 N（传了 `sort_by` 就按它），"
                        "图下会写明「仅显示前 N 项 · 共 M 项」。"
                        "**数据带时间轴时会被忽略**——截时间序列等于丢掉一段时间；"
                        "只想看最近 N 期就在取数或沙箱里先筛好再画。"
                    ),
                },
                "sort_by": {
                    "type": "string",
                    "description": (
                        "（可选）限行前按此数值列降序排. 例如 fetch 出的全量行里按'金额'拿 top 10 柱状图. "
                        "⚠️ 它管的是**行怎么排**，不是**画哪几列**——要指定画哪些列请用 `series`."
                    ),
                },
                "anchor_heading": {
                    "type": "string",
                    # canvas-update-turn batch B2-4: old description promised title-matched ordering during compose, but the sole consumer
                    # canvas_composer.build_api_chart_refs has no callers (dead wiring); models pass it yet charts still pile up at the end.
                    # Keep the parameter (removal would break calls still passing it); make the promise truthful and point to effective body anchors.
                    "description": (
                        "（可选）只作标记，**不会**移动图的位置。要让图出现在某个论点旁边，"
                        "在 write_report 的正文里该论点之后独占一行写 `[figure:<本工具返回的 section_id>]`。"
                    ),
                },
                **schema_properties(),
                "presentation_plan": evidence_notes_schema(),
            },
        },
    )


def _chart_request_needs_series(args: dict[str, Any]) -> bool:
    hint = str(args.get("hint") or "").strip().lower()
    title = f" {args.get('title') or ''} ".lower()
    return hint in {"trend", "compare"} or any(h.lower() in title for h in _SERIES_TITLE_HINTS)


def _title_implies_time_progression(args: dict[str, Any]) -> bool:
    """Title/hint implies a temporal PROGRESSION (走势/月度…, or hint='trend') that
    genuinely needs ≥2 time points. Narrower than _chart_request_needs_series: it
    excludes 同比/环比/vs and hint='compare', which can legitimately be a single
    row. Used to (a) enable artifact reselect for an explicit scalar-for-trend
    mistake and (b) gate the one-row-scalar safety-net — see _add_chart
    (dingsiyan 2026-07-07)."""
    if str(args.get("hint") or "").strip().lower() == "trend":
        return True
    title = str(args.get("title") or "")
    return any(h in title for h in _TREND_PROGRESSION_HINTS)


def _desired_metric_keys(args: dict[str, Any]) -> tuple[str, ...]:
    text = f"{args.get('title') or ''} {args.get('subtitle') or ''}".lower()
    keys: list[str] = []
    for key, terms in _METRIC_TERMS:
        if any(_contains_search_term(text, term) for term in terms):
            keys.append(key)
    return tuple(keys)


def _has_time_axis(cols: list[ChartColumn]) -> bool:
    for col in cols:
        key = str(getattr(col, "key", "") or "")
        header = str(getattr(col, "header", "") or "")
        lower = f"{key} {header}".lower()
        if getattr(col, "type", None) == "date":
            return True
        if any(h.lower() in lower for h in _TIME_COLUMN_HINTS):
            return True
    return False


def _artifact_signature(art: Any, cols: list[ChartColumn]) -> str:
    filename = str(getattr(art, "filename", "") or "")
    col_text = " ".join(f"{getattr(col, 'key', '')} {getattr(col, 'header', '')}" for col in cols)
    return f"{filename} {col_text}".lower()


def _chart_source_score(
    *,
    args: dict[str, Any],
    art: Any,
    rows: list[dict[str, Any]],
    cols: list[ChartColumn],
) -> int:
    need_series = _chart_request_needs_series(args)
    metric_keys = _desired_metric_keys(args)
    title = str(args.get("title") or "").lower()
    filename = str(getattr(art, "filename", "") or "").lower()
    signature = _artifact_signature(art, cols)
    score = 0

    if metric_keys:
        desired_terms = {term.lower() for key, terms in _METRIC_TERMS if key in metric_keys for term in terms}
        for metric_key in metric_keys:
            wanted_terms = next((terms for key, terms in _METRIC_TERMS if key == metric_key), ())
            if any(_contains_search_term(signature, term) for term in wanted_terms):
                score += 80
            else:
                score -= 60
            for bad in _METRIC_EXCLUSION_TERMS.get(metric_key, ()):
                bad_l = bad.lower()
                if bad_l in desired_terms:
                    continue
                if _contains_search_term(signature, bad_l):
                    score -= 60

    if need_series:
        score += 100 if len(rows) >= 2 else -200
        if _has_time_axis(cols):
            score += 40
        if any(h.lower() in filename for h in _MONTHLY_FILENAME_HINTS):
            score += 45
        elif "月" in filename and len(rows) <= 1:
            score -= 20

    years = set(re.findall(r"20\d{2}", title))
    if years:
        signature_years = set(re.findall(r"20\d{2}", signature))
        if years & signature_years:
            score += 25
        elif signature_years:
            score -= 80
        else:
            score -= 10

    title_segments = _segments_in_text(title)
    signature_segments = _segments_in_text(signature)
    if title_segments:
        score -= 80 * len(title_segments - signature_segments)
        score -= 35 * len(signature_segments - title_segments)
    else:
        score -= 25 * len(signature_segments)
    return score


def _candidate_filename_allows_reselect(args: dict[str, Any], filename: str) -> bool:
    """Skip obvious mismatches before reading/parsing candidate CSV files."""
    name = str(filename or "").lower()
    if not name:
        return True
    title = str(args.get("title") or "").lower()
    title_years = set(re.findall(r"20\d{2}", title))
    filename_years = set(re.findall(r"20\d{2}", name))
    if title_years and filename_years and not (title_years & filename_years):
        return False

    title_segments = _segments_in_text(title)
    filename_segments = _segments_in_text(name)
    if title_segments and filename_segments and not (title_segments & filename_segments):
        return False

    metric_keys = _desired_metric_keys(args)
    if metric_keys:
        desired_terms = {term.lower() for key, terms in _METRIC_TERMS if key in metric_keys for term in terms}
        has_desired = any(_contains_search_term(name, term) for term in desired_terms)
        bad_terms = {term.lower() for key in metric_keys for term in _METRIC_EXCLUSION_TERMS.get(key, ()) if term.lower() not in desired_terms}
        if bad_terms and not has_desired and any(_contains_search_term(name, term) for term in bad_terms):
            return False

    return True


async def _maybe_reselect_artifact_for_chart(
    *,
    aid: str,
    art: Any,
    rows: list[dict[str, Any]],
    cols: list[ChartColumn],
    args: dict[str, Any],
    ctx: ToolExecutionContext,
    allow_reselect: bool,
    selected_bytes: dict[str, bytes] | None = None,
) -> tuple[str, Any, list[dict[str, Any]], list[ChartColumn], dict[str, Any] | None]:
    """Recover from stale simple-id refs choosing a scalar/wrong-metric artifact.

    The model often calls ``add_chart(data_artifact_id="1")`` from an
    allowlist snapshot while newer BI outputs have just arrived. When the title
    asks for a trend/monthly chart, a one-row scalar artifact is almost certainly
    the wrong source. Prefer a same-session CSV whose filename/columns match the
    requested metric and carry a time series.
    """
    if not allow_reselect:
        return aid, art, rows, cols, None
    need_series = _chart_request_needs_series(args)
    metric_keys = _desired_metric_keys(args)
    current_score = _chart_source_score(args=args, art=art, rows=rows, cols=cols)
    if not ((need_series and len(rows) < 2) or (metric_keys and current_score < 20)):
        return aid, art, rows, cols, None
    if ctx.artifact_store is None or not ctx.session_id:
        return aid, art, rows, cols, None

    try:
        candidates = await ctx.artifact_store.list_for_session(
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            limit=100,
        )
    except Exception as exc:
        logger.debug("add_chart.reselect_list_failed turn=%s err=%s", ctx.turn_id, exc)
        return aid, art, rows, cols, None

    best: tuple[int, str, Any, list[dict[str, Any]], list[ChartColumn]] | None = None
    best_raw: bytes | None = None
    type_overrides = args.get("column_types") or {}
    for cand in candidates or []:
        cand_id = str(getattr(cand, "id", "") or "")
        cand_path = getattr(cand, "path", None)
        cand_name = str(getattr(cand, "filename", "") or "")
        cand_mime = str(getattr(cand, "mime", "") or "")
        if not cand_id or not cand_path:
            continue
        if cand_mime and cand_mime not in {"text/csv", "application/csv"} and not cand_name.endswith(".csv"):
            continue
        if not _candidate_filename_allows_reselect(args, cand_name):
            continue
        try:
            raw = await ctx.artifact_store.backend.read(cand_path)
            cand_rows, cand_cols = _parse_csv_with_types(raw, type_overrides=type_overrides)
        except Exception:  # noqa: S112
            continue
        cand_score = _chart_source_score(
            args=args,
            art=cand,
            rows=cand_rows,
            cols=cand_cols,
        )
        if best is None or cand_score > best[0]:
            best = (cand_score, cand_id, cand, cand_rows, cand_cols)
            best_raw = raw

    if best is None:
        return aid, art, rows, cols, None

    best_score, best_id, best_art, best_rows, best_cols = best
    min_score = 80 if need_series else 30
    if best_id != aid and best_score >= max(min_score, current_score + 50):
        if selected_bytes is not None and best_raw is not None:
            selected_bytes.clear()
            selected_bytes[best_id] = best_raw
        logger.info(
            "add_chart.reselected_artifact turn=%s title=%r from=%s to=%s score=%s>%s",
            ctx.turn_id,
            args.get("title"),
            aid,
            best_id,
            best_score,
            current_score,
        )
        return (
            best_id,
            best_art,
            best_rows,
            best_cols,
            {
                "from_artifact_id": aid,
                "from_filename": getattr(art, "filename", None),
                "to_filename": getattr(best_art, "filename", None),
                "score": best_score,
                "previous_score": current_score,
                "reason": "series_metric_mismatch",
            },
        )
    return aid, art, rows, cols, None


#: Valid target values. **No both**: under the intersection model it is equivalent to omission
#: (candidate 4 §4.3); adding it just gives models another equivalent choice to guess.
_SURFACE_TARGETS = ("chat", "canvas")


def resolve_surface_target(bus: Any, raw: Any) -> str | None:
    """单张图的 `target` × 轮级判定 ⇒ 这张图**实际**的去向。

    规则一句话（候选 4 §4.1 拍板）：**`target` 只能收窄曝光，不能放宽**
    ——曝光面 = 所有声明的交集。

    | 轮级判定        | target   | 结果                                  |
    | --------------- | -------- | ------------------------------------- |
    | `chat`          | 任意     | `chat`（`canvas` 被忽略并记日志）     |
    | `canvas` / 无   | `chat`   | **`chat`**（报告轮里的随手核对图）    |
    | `chat` 之外     | `canvas` | `canvas`（挡住轮末证据兜底把它藏掉）  |
    | 任意            | 不传     | `None` ⇒ 沿用轮级判定，**逐字不变**   |

    为什么 `chat` 轮要忽略 `target='canvas'`：轮级判定的来源是用户原话或 Lead 的
    明说，层级高于 peer 画单张图时填的参数——让一个工具参数推翻用户原话是危险的。
    反过来「收窄」永远安全，所以放行。
    """
    target = str(raw or "").strip().lower()
    if target not in _SURFACE_TARGETS:
        return None  # Missing/invalid → retain turn-level verdict (LLM-writable, defensively narrow)
    turn = getattr(bus, "_early_surface_decision", None) if bus is not None else None
    if target == "canvas" and turn == "chat":
        logger.info("chat_surface.target_canvas_ignored turn_decision=chat (target 不能放宽曝光)")
        return None
    return target


async def _hide_if_early_chat(ctx: Any, bus: Any, turn_id: str | None) -> None:
    """chat-canvas-surface 2026-08 —— 早判定为 chat 时，section **建完立刻置位**。

    只压 SSE 事件不够：`hidden_for_chat_mode` 是持久化列（`canvas_store.py:157`）
    且 canvas API 按它过滤（`routes/canvas.py:702/1105`）。用户**在轮次中途 F5**
    时画布走的是 API 而不是事件流——那时轮末的 D6 还没跑、标志还没置位，
    图照样冒出来。置位后中途刷新也正确。

    复用既有原语 `mark_turn_hidden_for_chat_mode`（按 turn 批量置位，重复调用
    只是无副作用的重复 UPDATE），不新建存储层机制。轮末若翻案，
    `lead_orchestrator` 那边会调 `unhide_turn` 精确反向。

    best-effort：置位失败绝不能挡住建图本身（图还在、只是画布上多露一会儿）。
    """
    if not turn_id or bus is None or not getattr(bus, "_early_chat_surface", False):
        return
    store = getattr(ctx, "canvas_store", None)
    if store is None:
        return
    try:
        await store.mark_turn_hidden_for_chat_mode(turn_id, "early: declared chat before draw")
    except Exception as exc:  # noqa: BLE001 - marking failure must not block chart creation
        logger.warning("chat_surface.early_hide_failed turn=%s err=%s", turn_id, exc)


async def _apply_surface_target(ctx: Any, bus: Any, section_id: str, raw_target: Any) -> bool:
    """落库单张图的去向声明；返回**是否压掉该段的「露面」事件**。

    压事件与落库必须成对（候选 2 §4.2.2 的同一条教训）：只压事件的话，用户
    轮次中途 F5 走 canvas API 仍会把图捞回来；只落库不压事件的话，live 那一帧
    图会闪一下才被后续状态收掉。
    """
    target = resolve_surface_target(bus, raw_target)
    if target is None:
        return False
    store = getattr(ctx, "canvas_store", None)
    if store is not None:
        try:
            await store.set_section_surface_target(section_id, target)
        except Exception as exc:  # noqa: BLE001 — persistence failure must never block chart creation itself
            logger.warning("chat_surface.surface_target_persist_failed sec=%s err=%s", section_id, exc)
    return target == "chat"


async def _add_chart(
    args: dict[str, Any],
    ctx: ToolExecutionContext,
    *,
    _prepare_only: bool = False,
    _prepared_frame: bool = False,
    _analytical: dict[str, Any] | None = None,
    _preserve_observations: bool = False,
    _composed_views: list[dict[str, Any]] | None = None,
    _layout: dict[str, Any] | None = None,
    _group_title: str | None = None,
    _dataset_cache: list[_PreparedChartDataset] | None = None,
) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}
    misplaced = sorted(
        set(args)
        & {
            "evidence_notes",
            "view_bindings",
            "bindings",
            "interactions",
            "scale_groups",
            "annotations",
            "table_trends",
            "guide_steps",
            "dataset",
            "text_dependencies",
            "_export_axis_domains",
            "_export_table_trend",
            "_export_analytical",
            "analytical_views",
        }
    )
    misplaced = [key for key in misplaced if key not in {"evidence_notes", "guide_steps"}]
    if misplaced:
        return {
            "error": "Place authoring notes/interactions inside presentation_plan, not at the add_chart root. Server-owned dataset/bindings cannot be authored.",
            "error_kind": "invalid_chart_expression",
            "code": "plan_shape",
            "path": "/" + misplaced[0],
        }
    plan_input = None
    if "presentation_plan" in args or args.get("evidence_notes") or "guide_steps" in args:
        try:
            fields = {key: args[key] for key in ("evidence_notes", "guide_steps") if key in args and (key != "evidence_notes" or args[key])}
            plan_input = parse_plan_input(args.get("presentation_plan", {"version": 1}), root_fields=fields)
        except ChartPlanError as exc:
            return {
                "error": str(exc),
                "error_kind": "invalid_chart_expression",
                "code": exc.code,
                "path": exc.path,
                **({"corrective_ids": exc.corrective_ids} if exc.corrective_ids else {}),
            }
    if "evidence_notes" in args or "guide_steps" in args:
        args = {key: value for key, value in args.items() if key not in {"evidence_notes", "guide_steps"}}
    if ("views" in args or "derived_columns" in args) and (
        not isinstance(args.get("hint", "raw"), str) or args.get("hint", "raw") not in VALID_HINTS
    ):
        return {"error": "Use an analysis hint such as compare or raw; heatmap belongs in force_kind", "error_kind": "invalid_chart_expression"}
    if ("views" in args or "derived_columns" in args) and args.get("force_kind") is not None and not isinstance(args["force_kind"], str):
        return {"error": "force_kind must be a native chart kind string", "error_kind": "invalid_chart_expression"}
    if "views" in args:
        fallbacks: list[dict[str, str]] = []
        try:
            views, layout = composition(args, fallbacks=fallbacks)
        except (ValueError, TypeError, RecursionError) as exc:
            return {"error": str(exc), "error_kind": "invalid_chart_expression"}
        base = {
            key: value
            for key, value in args.items()
            if key not in {"views", "layout", "series", "force_kind", "sort_by", "limit", "hint", "title", "subtitle", "presentation_plan"}
        }
        prepared: list[dict[str, Any]] = []
        dataset_cache: list[_PreparedChartDataset] = []
        frame_targets = {vid for item in (plan_input or {}).get("interactions", []) if item["preset"] == "frame_player" for vid in item["view_ids"]}
        observation_targets = {
            vid
            for item in (plan_input or {}).get("interactions", [])
            for vid in ([item["from_view"], *item["to_views"]] if item["preset"] == "linked_focus" else item.get("view_ids", []))
        }
        analytical_targets = {item["view_id"]: item for item in (plan_input or {}).get("analytical_views", [])}
        for view in views:
            result = await _add_chart(
                {**base, **{key: value for key, value in view.items() if key != "id"}},
                ctx,
                _prepare_only=True,
                _dataset_cache=dataset_cache,
                _prepared_frame=view["id"] in frame_targets,
                _analytical=analytical_targets.get(view["id"]),
                _preserve_observations=view["id"] in observation_targets,
            )
            if not isinstance(result, dict) or "_prepared_spec" not in result:
                return {**result, "view_id": view["id"]}
            spec_dict = spec_to_dict(result["_prepared_spec"])
            spec_dict.pop("source_meta", None)
            prepared.append({"id": view["id"], "kind": "chart", "spec": spec_dict})
        if len({view["spec"].get("data_artifact_id") for view in prepared}) != 1:
            return {
                "error": "All composed views must resolve to the same dataset; fetch the intended dataset explicitly",
                "error_kind": "invalid_chart_expression",
            }
        if len(json.dumps(prepared, ensure_ascii=False).encode()) > 1_000_000:
            return {
                "error": "Combined native views exceed 1 MB; reduce views or compute a smaller dataset upstream",
                "error_kind": "invalid_chart_expression",
            }
        if len(views) == 1 and not analytical_targets:
            result = await _add_chart(
                {
                    **base,
                    **{key: value for key, value in views[0].items() if key != "id"},
                    **({"presentation_plan": plan_input} if plan_input else {}),
                },
                ctx,
                _dataset_cache=dataset_cache,
            )
            result = {**result, "normalized": "single_view"}
        else:
            result = await _add_chart(
                {
                    **base,
                    **{key: value for key, value in views[0].items() if key != "id"},
                    **({"presentation_plan": plan_input} if plan_input else {}),
                },
                ctx,
                _composed_views=prepared,
                _prepared_frame=views[0]["id"] in frame_targets,
                _analytical=analytical_targets.get(views[0]["id"]),
                _preserve_observations=views[0]["id"] in observation_targets,
                _layout=layout,
                _group_title=str(args.get("title") or views[0]["title"]).strip()[:60],
                _dataset_cache=dataset_cache,
            )
        if fallbacks and "error" not in result:
            result = {**result, "presentation_fallbacks": [*result.get("presentation_fallbacks", []), *fallbacks]}
        return result
    if "layout" in args:
        return {"error": "layout requires views", "error_kind": "invalid_chart_expression"}

    aid = str(args.get("data_artifact_id") or "").strip()
    if not aid:
        return {
            "error": "data_artifact_id required (先 `bi_query` 拿 `_ref_artifact_id`, 或 `python_sandbox` 里 `to_csv` 出数据取 output_artifacts[0].artifact_id)"
        }
    input_aid = aid
    # Add missing art_ prefix to bare 12-digit hex in place; observed in 116 live testing.
    # Only a prefix separates it from validity, so no reason to fail the call. Owner checks below still apply.
    if _BARE_ARTIFACT_HEX_RE.match(aid):
        aid = f"art_{aid}"
    aid, simple_ref_kind = resolve_simple_data_ref(aid, ctx)
    simple_data_ref_resolved = simple_ref_kind == "data" and _SIMPLE_INT_REF_RE.match(input_aid) is not None and aid != input_aid
    if not _VALID_DATA_ARTIFACT_ID_RE.match(aid):
        logger.info(
            "add_chart.invalid_artifact_id_format turn=%s aid=%r",
            getattr(ctx, "turn_id", ""),
            input_aid,
        )
        hint = (
            f"data_artifact_id {input_aid!r} 不是合法格式 (expected art_xxxxxxxxxxxx, "
            "12 hex chars). 真实 artifact_id 来源: `bi_query` 返的 `_ref_artifact_id`, "
            "或 `python_sandbox` 返的 `output_artifacts[].artifact_id`. "
            "不要传行号 / 列名 / 标题之类的值."
        )
        if _SIMPLE_INT_REF_RE.match(input_aid):
            if simple_ref_kind:
                hint += (
                    f" 这个 simple id 在当前 allowlist 中是 {simple_ref_kind!r}, "
                    "不是 data artifact. 已有图表请在 evidence 里引用 chart_section_id, "
                    "不要传给 add_chart.data_artifact_id."
                )
            else:
                hint += ' 如果你想引用 <available_refs> 里的 data id, 它必须出现在 <data kind="data" ...> 条目中; 否则请重新调用 `bi_query` 取数.'
        # ★★ Merely saying the ID is wrong leaves models guessing: 116 t_17e216c2316b guessed **52 times**
        # and produced no charts. List this turn's **available data IDs immediately** so it can fix the call in one step;
        # if none exist, explicitly require fetching data first, or it assumes a typo and repeatedly retries.
        hint += _available_data_hint(ctx)
        return {
            "skipped": "invalid_artifact_id_format",
            "data_artifact_id": input_aid,
            "hint": hint,
        }
    if ctx.artifact_store is None:
        return {"error": "artifact_store not configured on ctx"}
    if ctx.canvas_store is None or not ctx.session_id:
        return {"error": "canvas_store/session_id not configured on ctx"}

    # #54 permanent fix (2026-04-29): owner check prevents unauthorized use of artifact IDs recalled from another user/session.
    cached = _dataset_cache[0] if _dataset_cache else None
    art = cached.artifact if cached else await ctx.artifact_store.get(aid, check_created_by=ctx.user_id)
    if art is None:
        return {"error": f"artifact {aid!r} not found"}

    if cached:
        aid = art.id

    if cached:
        rows, cols = copy.deepcopy((cached.rows, cached.columns))
        raw_bytes = cached.raw_bytes
        _non_finite = cached.non_finite.copy()
        reselect_meta = cached.reselect_meta
    else:
        # 1. Read CSV and infer types (backend.read routes to local / S3).
        try:
            raw_bytes = await ctx.artifact_store.backend.read(art.path)
            _non_finite: dict[str, int] = {}
            rows, cols = _parse_csv_with_types(
                raw_bytes,
                type_overrides=args.get("column_types") or {},
                non_finite=_non_finite,
            )
        except Exception as e:
            return {"error": f"failed to parse artifact CSV: {type(e).__name__}: {e}"}
        # #5 fix (dingsiyan 2026-07-07): also allow reselect when the title/hint asks
        # for a time-progression chart (走势/趋势/月度… or hint='trend'). Previously
        # gated to resolved simple-int refs only, so an analyst passing a REAL artifact
        # id that happens to be a 1-row scalar (the common prod shape) skipped recovery
        # and rendered a misleading one-point KPI titled "…走势". The inner trigger +
        # score threshold still protect a genuinely-good explicit choice from mutation.
        # Merge (Feishu follow-up #5): explicit art_xxx for non-time-series titles still **must not** be reselected (no evidence tampering;
        # fixed by test_real_artifact_id_does_not_reselect); such series conflicts use the warning below
        # for agent self-correction. Only time-series titles enable reselect/error through dingsiyan's gate above.
        _aid_before_reselect = aid
        selected_bytes = {aid: raw_bytes}
        aid, art, rows, cols, reselect_meta = await _maybe_reselect_artifact_for_chart(
            aid=aid,
            art=art,
            rows=rows,
            cols=cols,
            args=args,
            ctx=ctx,
            allow_reselect=simple_data_ref_resolved or _title_implies_time_progression(args),
            selected_bytes=selected_bytes,
        )
        raw_bytes = selected_bytes[aid]
        if aid != _aid_before_reselect:
            _non_finite.clear()  # Count belongs to the replaced file

    unsafe_keys = [column.key for column in cols if column.type == "numeric" and any(_unsafe_integer_token(row.get(column.key)) for row in rows)]
    if unsafe_keys and (args.get("force_kind") != "table" or args.get("derived_columns") or args.get("presentation_plan")):
        return {
            "error": "Exact integers exceed the native chart numeric range in columns "
            + ", ".join(unsafe_keys)
            + ". Use force_kind=table with hint=raw and no derived columns or presentation plan to preserve the original integers.",
            "error_kind": "unsupported_exact_numeric_plot",
            "columns": unsafe_keys,
        }
    if unsafe_keys:
        hint = "raw"

    units = args.get("column_units", {})
    numeric_columns = {column.key: column for column in cols if column.type == "numeric"}
    if not isinstance(units, dict) or any(
        key not in numeric_columns
        or not isinstance(unit, str)
        or not unit.strip()
        or len(unit) > 32
        or (unit.strip() == "%" and not numeric_columns[key].percent_scale)
        for key, unit in units.items()
    ):
        return {
            "error": "column_units must map existing numeric columns to nonempty units of at most 32 characters; % requires an explicit percentage shape",
            "error_kind": "invalid_chart_expression",
            "path": "/column_units",
        }

    for key, unit in units.items():
        if numeric_columns[key].percent_scale in {"fraction", "points"} and unit.strip() != "%":
            return {
                "error": "A declared percentage level requires unit %. Fraction/points describe storage. For a percentage-point difference, use a numeric column with its stated unit instead.",
                "error_kind": "invalid_chart_expression",
                "path": "/column_units/" + key,
            }

    derived_bytes: bytes | None = None
    calculation_metadata: list[dict[str, Any]] | None = None
    calculation_missing: dict[str, int] | None = None
    derived_row_count = len(rows)
    if cached:
        derived_bytes = cached.derived_bytes
        calculation_metadata = copy.deepcopy(cached.calculation_metadata)
        calculation_missing = cached.calculation_missing
        derived_row_count = cached.derived_row_count
    elif "derived_columns" in args:
        try:
            _apply_column_units(cols, units)
            _enrich_units(cols, ctx, rows, artifact=art)
            rows, cols, calculation_metadata, calculation_missing = derive_columns(args["derived_columns"], rows, cols)
            csv_output = _io.StringIO()
            writer = _csv.DictWriter(csv_output, fieldnames=[column.key for column in cols])
            writer.writeheader()
            writer.writerows(rows)
            derived_bytes = csv_output.getvalue().encode("utf-8")
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            return {"error": str(exc), "error_kind": "invalid_chart_expression"}
    display_fallbacks = apply_column_display(cols, args.get("column_display"))
    if _dataset_cache is not None and not _dataset_cache:
        _dataset_cache.append(
            _PreparedChartDataset(
                art,
                copy.deepcopy(rows),
                copy.deepcopy(cols),
                _non_finite.copy(),
                reselect_meta,
                derived_bytes,
                copy.deepcopy(calculation_metadata),
                calculation_missing,
                derived_row_count,
                raw_bytes,
            )
        )
    if _prepare_only or calculation_metadata:
        numeric_keys = {column.key for column in cols if column.type == "numeric"}
        requested = args.get("series", [])
        if not isinstance(requested, list) or any(not isinstance(key, str) or key not in numeric_keys for key in requested):
            return {"error": f"series must name existing numeric columns: {sorted(numeric_keys)}", "error_kind": "invalid_chart_expression"}
        if args.get("sort_by") is not None and (not isinstance(args["sort_by"], str) or args["sort_by"] not in numeric_keys):
            return {"error": f"sort_by must name an existing numeric column: {sorted(numeric_keys)}", "error_kind": "invalid_chart_expression"}

    plan_data_columns = copy.deepcopy(cols) if plan_input else None
    plan_data_rows = rows if plan_input else None

    # Apply optional sorting and limits before inference so a large ranking can
    # remain a chart. Never silently take the first rows as a top-N result or remove
    # the end of a time series. Disclose truncation and preserve the original row count.
    sort_by = args.get("sort_by")
    sorted_by_col = bool(sort_by) and any(c.key == sort_by for c in cols)
    if sorted_by_col:
        rows.sort(
            key=lambda r: r.get(sort_by) if isinstance(r.get(sort_by), (int, float)) else float("-inf"),
            reverse=True,
        )
    limit = args.get("limit")
    try:
        limit_n = int(limit) if limit not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        limit_n = None
    rows_before_limit = len(rows)
    limit_note: str | None = None

    # 1.6 Wide-table column pruning (#61 + #76): nonempty hint with **too many numeric columns to plot** → (category + numeric).
    # Covers every nonempty hint (rank/share/compare/trend/correlation/composition/raw).
    #
    # Gate changed from total columns >6 to numeric columns ≥ table-fallback threshold (chart-config-ux batch 1 ②).
    # Previously the gates were independently defined, leaving a gap: pruning checked **total columns >6**, while infer
    # fell back at **numeric columns >=4** (_MAX_SERIES +1). Wide tables with 4–6 columns missed pruning yet
    # hit table fallback. 39 charts in the 116 live window suffered, including 26 with ≤30 rows,
    # fully plottable by row count but rejected solely for column count. A real case had five versions of one metric
    # （`share_cy` / `share_ly` / `share_cy_pct` / `share_ly_pct` / `delta_pp`），
    # intended as two bars + one delta, but yielded an 11-row, six-column table.
    #
    # Import the threshold directly from infer; do not add a separate constant. Both gates need one source,
    # or changing one opens another gap.
    hint = str(args.get("hint") or "raw")
    title = str(args.get("title") or "数据图表").strip()[:60]
    # Explicit series takes precedence over heuristic pruning; contract gap found in 116 prod 1.0.79 dogfood:
    # previously models **could not specify plotted columns** (only sort_by selected one). Four charts from the same
    # 18-column table differed only in title; backend always pruned to province/revenue. Three promised July vs June
    # or month-over-month changes but lacked a second series. Accurate heuristics cannot fix inability to express intent,
    # so provide explicit expression first and demote heuristics to fallback.
    wide_pivot_cut: SeriesCut | None = None
    if _analytical:
        from bowhead_core.chart.analytical import MAX_ROWS, analytical_columns

        retained = analytical_columns(_analytical)
        # Explicit extra numeric fields remain available for original-row inspection.
        # The analytical bindings, not ordinary series inference, select plotted marks.
        retained.update(args.get("series") or [])
        missing = retained - {column.key for column in cols}
        if missing or len(rows) > MAX_ROWS or limit_n:
            reason = (
                f"Missing columns: {sorted(missing)}"
                if missing
                else f"Prepared rows: {len(rows)}, maximum: {MAX_ROWS}"
                if len(rows) > MAX_ROWS
                else "Remove limit to retain complete analytical observations"
            )
            return {
                "error": reason,
                "error_kind": "invalid_chart_expression",
                "code": "binding_ineligible",
                "path": "/presentation_plan/analytical_views",
            }
        cols = [column for column in cols if column.key in retained]
        rows = [{key: row.get(key) for key in retained} for row in rows]
    explicit = None if _analytical else _explicit_series(args, rows, cols, preserve_dimensions=_prepared_frame or _preserve_observations)
    if explicit:
        rows, cols = explicit
    elif not _analytical and hint != "raw" and _needs_col_pruning(cols):
        rows, cols, wide_pivot_cut = _prune_cols_for_hint(rows, cols, hint=hint, sort_by=sort_by)

    # Apply the limit after column pruning so top-N uses the metric actually plotted.
    # Sorting before pruning could rank by a numeric column that the chart removes.
    # Do not truncate a time axis as though it were a ranking; disclose any truncation.
    if limit_n and limit_n > 0 and len(rows) > limit_n:
        # Time-axis criterion shares infer's source, but here asks only whether this column has multiple values;
        # _constant_dim_keys returns empty with one dimension, incorrectly treating an entire single-day column as a time axis.
        has_time_axis = any(c.type == "date" and len({str(r.get(c.key)) for r in rows}) > 1 for c in cols)
        if has_time_axis:
            # No correct truncation exists on a time axis: value-based choice disrupts time; original-order truncation cuts recent months.
            # Table fallback without truncation has a separate time-axis remedy (line family uses time-point count; see _MAX_LINE_POINTS);
            # do not change chart kind by discarding data.
            #
            # ⚠ This must precede sorted_by_col. Previously sort_by bypassed it entirely, so 59 provinces ×12 months
            # with sort_by + limit=400 were cut to 400 rows (34 provinces) before infer.
            # The series gate could count only those 34, making the chart caption say 34 total series
            # instead of the actual 59 provinces (observed in re-review). sort_by is itself meaningless on a time axis:
            # sorting by value and taking the first N selects high-valued rows scattered across months, destroying the time axis.
            limit_note = f"数据带时间轴，`limit={limit_n}` 已忽略：截时间序列会丢掉一段时间，图按全部 {len(rows)} 行画。"
        elif sorted_by_col:
            rows = rows[:limit_n]  # Already sorted by this column; first N is the requested top N
        else:
            # Rankings: take **actual** top N by main numeric column, sharing infer._take_top_n instead of copying it.
            rows = _take_top_n(rows, [c for c in cols if c.type == "numeric"], limit_n)

    # #5 safety-net (dingsiyan 2026-07-07): a temporal-progression chart (走势/月度…
    # or hint='trend') whose data is a single scalar row is a wrong-artifact mistake
    # — the query aggregated to a total and dropped the time dimension. Reselect
    # above tries to recover a real in-session series; if none exists, refuse to
    # render a misleading one-point KPI titled "…走势" and hand the analyst an
    # actionable error so it re-fetches a time series. 同比/环比/vs are excluded via
    # the narrower _title_implies_time_progression (they can legitimately be 1 row).
    if len(rows) < 2 and _title_implies_time_progression(args):
        return {
            "error": (
                f"标题 {title!r} 要求时间序列(走势/趋势/月度)，但数据源 {aid} 只有 "
                f"{len(rows)} 行——这是聚合后的单值/总额，缺少按月或按时间的维度，无法"
                "画走势。请先用 `bi_query` 取一份带时间粒度的序列(datarc 原生语法, "
                "1 条即可: '2025年 营业额 每月' / '今年 GMV 每季度'——❌ 禁逐期拆问)，"
                "再调 add_chart；若只想展示单个总值，请去掉标题里的"
                "'走势/趋势/月度'改画 KPI。"
            ),
            "error_kind": "chart_series_needs_time_dimension",
        }

    # 1.9 Look up source metadata by column name to enrich units/percent shape.
    # CSV carries no units; without enrichment frontend guesses from names/value ranges, but percent shape cannot be inferred from values alone
    # (achievement 1.02 is 102%, market share 2.45 is 2.45%; ranges overlap). Leave unchanged on lookup failure,
    # retaining the frontend name fallback.
    _apply_column_units(cols, units)
    if not calculation_metadata:
        _enrich_units(cols, ctx, rows, artifact=art)

    # Values and names cannot prove a percentage interpretation or contradict two
    # valid declarations merely because their observations happen to differ 100x.
    _mark_unknown_percent_shape(cols)

    # 2. Infer ChartSpec.
    from bowhead_core.chart.prepared_frames import MAX_FRAME_ROWS

    if _analytical:
        from bowhead_core.chart.types import ChartSpec

        spec = ChartSpec(
            kind=args.get("force_kind"),
            title=title,
            subtitle=str(args.get("subtitle") or "").strip() or None,
            rows=rows,
            columns=cols,
            data_artifact_id=aid,
            row_count=len(rows),
        )
    else:
        spec = infer_chart_spec(
            rows=rows,
            columns=cols,
            hint=hint,
            title=title,
            subtitle=str(args.get("subtitle") or "").strip() or None,
            data_artifact_id=aid,
            force_kind=args.get("force_kind"),
            **({"inline_rows_limit": MAX_FRAME_ROWS} if _prepared_frame else {}),
        )

    # 2.3 Disclose wide-table pivot series pruning too, under the same rules as long-table _cap_time_series.
    #
    # 116 dogfood 2026-09-24 t_b06a7810265a: a 38-category wide table was pruned to 12 columns/lines,
    # but subtitle and body claimed all 38 first-level categories and their monthly-revenue lines,
    # KPI card said 38 categories, and analysis described the other 35 lines or 34 overlapping near the bottom.
    # **Three labels plus two analytical sentences described lines never plotted**. The model was not inventing them:
    # add_chart output was its sole basis for body text, and pruning only logged without returning any information.
    #
    # Populating these two fields restores three consumers together: web captions, Feishu cards, and the model's warning
    # against calling it the entire dataset (see capped_series(spec) below). Long-table path already did this;
    # the mechanism existed but this branch was unwired ([[feedback_correct_but_out_of_reach]]).
    #
    # ⚠ Do not overwrite infer's existing values; _cap_time_series is the more specific cut and takes precedence.
    #   This condition is **currently unreachable**: wide pruning leaves numeric columns ≤ _MAX_SERIES_SAME_FAMILY (12),
    #   while _cap_time_series acts only at seriesCount > _MAX_TIME_SERIES (15); the cuts are mutually exclusive.
    #   Keep it because exclusion depends only on the two constants' current relative sizes, not a guarantee
    #   ([[feedback_correctness_by_coincident_constants]]). This premise is fixed by
    #   test_the_two_series_cuts_cannot_both_fire, which fails if 12 is raised enough.
    if wide_pivot_cut is not None and spec.series_shown is None and spec.series_total is None:
        spec.series_shown, spec.series_total = wide_pivot_cut.shown, wide_pivot_cut.total
        # Same ranking destination as long-table path (not a dataclass field, so neither persisted nor sent to frontend)
        spec._series_rank = wide_pivot_cut.rank  # type: ignore[attr-defined]

    # 2.4 Disclose limit truncation: restore row_count to original total and set top_n to rows actually plotted;
    # frontend captions show first N of M items, matching backend top-N selection rules.
    if rows_before_limit > len(rows):
        spec.row_count = rows_before_limit
        if spec.kind != "table":  # Table footer already gives counts; marking repeats them
            spec.top_n = len(spec.rows or [])

    # 2.5 P3.6: inject source_meta for chart's top source row (tool / query / artifact_id / turn_id).
    # Batch C (report-edit-audit P2-3): tool uses the **data artifact's producer** (art.source, e.g. upstream query
    # or sandbox computation), no longer hardcoded add_chart, which merely places a chart on Canvas.
    # Showing that tool leaked implementation details (confirmed in review) and disagreed with the data-source list.
    source_query = str(args.get("query") or "").strip() or str(args.get("title") or "").strip() or None
    spec.source_meta = {
        "id": aid,
        "tool": str(getattr(art, "source", "") or "").strip() or "add_chart",
        "query": source_query,
        "artifact_id": aid,
        "turn_id": ctx.turn_id,
        "tool_call_id": getattr(ctx, "tool_call_id", None),
    }
    if reselect_meta:
        spec.source_meta["resolved_from_artifact_id"] = reselect_meta["from_artifact_id"]
        spec.source_meta["resolved_from_filename"] = reselect_meta.get("from_filename")
        spec.source_meta["resolved_reason"] = reselect_meta["reason"]

    # Sprint ι (PRD §4.C.2): if backend's auto-chart fallback called us, surface
    # uncertainty via subcaption. Caller passes auto_charted=True (see auto_chart.py).
    if args.get("auto_charted"):
        spec.auto_charted = True

    spec.derived_columns = calculation_metadata
    spec.calculation_missing = calculation_missing or None
    if _prepare_only:
        return {"_prepared_spec": spec}
    if _composed_views is not None:
        spec.views, spec.layout = _composed_views, _layout
        spec.title = _group_title or spec.title

    note_fallbacks: list[dict[str, str]] = list(display_fallbacks)
    if plan_input:
        try:
            plan_columns = plan_data_columns or []
            _apply_column_units(plan_columns, units)
            apply_column_display(plan_columns, args.get("column_display"))
            if not calculation_metadata:
                _enrich_units(plan_columns, ctx, plan_data_rows, artifact=art)
            notes = await verify_plan_notes(plan_input, plan_columns, ctx)
            note_fallbacks += [
                {
                    "code": "note_excerpt_normalized",
                    "path": f"/presentation_plan/evidence_notes/{index}/excerpt",
                    "message": "Equivalent JSON formatting was replaced with the exact source text",
                }
                for index, (submitted, verified) in enumerate(zip(plan_input.get("evidence_notes", []), notes, strict=True))
                if submitted["excerpt"] != verified["excerpt"]
            ]
            spec.presentation_plan = compile_notes_plan(
                spec_to_dict(spec),
                notes,
                artifact_id=aid,
                raw=derived_bytes if derived_bytes is not None else raw_bytes,
                columns=plan_columns,
                input_plan=plan_input,
                rows=plan_data_rows or [],
            )
            from bowhead_core.chart.presentation_plan import validate_stored_notes_plan

            validate_stored_notes_plan(spec_to_dict(spec))
        except ChartPlanError as exc:
            return {
                "error": str(exc),
                "error_kind": "invalid_chart_expression",
                "code": exc.code,
                "path": exc.path,
                **({"corrective_ids": exc.corrective_ids} if exc.corrective_ids else {}),
            }

    if len(json.dumps(spec_to_dict(spec), ensure_ascii=False).encode("utf-8")) > 1_000_000:
        return {
            "error": "The complete chart exceeds 1 MB; reduce views or prepare a smaller dataset upstream",
            "error_kind": "invalid_chart_expression",
        }

    async def materialize_calculation() -> None:
        if derived_bytes is not None:
            derived_artifact = await ctx.artifact_store.put_bytes(
                content=derived_bytes,
                filename="chart-calculation.csv",
                mime="text/csv",
                created_by=ctx.user_id,
                source="chart_calculation",
                kind="tool_data",
                session_id=ctx.session_id,
                workspace_id=ctx.workspace_id,
                created_by_turn=ctx.turn_id,
                derived_from=aid,
                origin_query=getattr(art, "origin_query", None),
                row_count=derived_row_count,
            )
            spec.data_artifact_id = derived_artifact.id
            if spec.presentation_plan:
                spec.presentation_plan["dataset"]["artifact_id"] = derived_artifact.id
            for view in _composed_views or []:
                view["spec"]["data_artifact_id"] = derived_artifact.id

    # 3. Persist to canvas_store.
    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )

    # D6: dedup — if THIS turn already appended a chart with the same title, don't emit a
    # second section (the D5 artifact thrash could fire add_chart twice: a 1-row
    # scratch kpi + the full series). Prefer the richer series: if the new chart has more
    # rows, patch the existing section in place (kpi→line upgrade); else skip the degenerate
    # new one. Best-effort: a store hiccup must never block charting.
    try:
        _prior_charts = await ctx.canvas_store.list_sections_appended_by_turn(
            ctx.turn_id,
            kinds=["chart"],
        )
    except Exception:
        _prior_charts = []

    def _prior_spec(sec: Any) -> dict[str, Any]:
        return (getattr(sec, "payload", None) or {}).get("spec") or {}

    def _view_identity(views: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if views is None:
            return None
        return [{**view, "spec": {key: value for key, value in view["spec"].items() if key != "data_artifact_id"}} for view in views]

    def _same_expression(prior: dict[str, Any]) -> bool:
        candidate = spec_to_dict(spec)
        if prior.get("subtitle") != spec.subtitle:
            return False
        # Same-source scratch-to-series correction remains valid for a compatible
        # metric. A shared title alone cannot stand in for a different chart request.
        if not any(
            (
                prior.get("views"),
                spec.views,
                prior.get("derived_columns"),
                spec.derived_columns,
                prior.get("presentation_plan"),
                spec.presentation_plan,
            )
        ):
            prior_columns = prior.get("columns") or []
            candidate_columns = candidate.get("columns") or []
            old_rows, new_rows = prior.get("rows") or [], spec.rows or []
            scratch_pair = (prior.get("kind") == "kpi" and len(old_rows) == 1 and len(new_rows) > 1) or (
                spec.kind == "kpi" and len(new_rows) == 1 and len(old_rows) > 1
            )
            if scratch_pair and not args.get("force_kind"):
                old_metrics = {column["key"]: column for column in prior_columns if column.get("type") == "numeric"}
                new_metrics = {column["key"]: column for column in candidate_columns if column.get("type") == "numeric"}
                common = old_metrics.keys() & new_metrics.keys()
                return bool(common) and all(old_metrics[key] == new_metrics[key] for key in common)
            presentation_keys = ("kind", "columns", "sort_by", "secondary_axis", "percent_stack", "show_legend", "show_data_labels")
            if any(prior.get(key) != candidate.get(key) for key in presentation_keys):
                return False
            # Only equivalent retained observations (or an ordered prefix) may
            # share a section. Different cuts/orderings must remain separate.
            shared_length = min(len(old_rows), len(new_rows))
            return old_rows[:shared_length] == new_rows[:shared_length]
        return (
            prior.get("kind") == spec.kind
            and prior.get("columns") == spec_to_dict(spec).get("columns")
            and prior.get("derived_columns") == spec.derived_columns
            and _view_identity(prior.get("views")) == _view_identity(spec.views)
            and prior.get("layout") == spec.layout
            and _plan_identity(prior.get("presentation_plan")) == _plan_identity(spec.presentation_plan)
        )

    def _plan_identity(plan: dict[str, Any] | None) -> dict[str, Any] | None:
        if plan is None:
            return None
        return {**plan, "dataset": {key: value for key, value in plan["dataset"].items() if key != "artifact_id"}}

    _dup = next(
        (
            s
            for s in _prior_charts
            if _prior_spec(s).get("title") == spec.title
            and (_prior_spec(s).get("source_meta") or {}).get("artifact_id", _prior_spec(s).get("data_artifact_id")) == aid
            and _same_expression(_prior_spec(s))
        ),
        None,
    )
    if _dup is not None:
        # ⚠ Compare **rows actually plotted**, not row_count: after limit, row_count is the original total
        # for total-M captions. Using it to judge completeness lets a chart plotting only ten of 1000 source rows
        # replace a complete 40-row chart (re-review observed reversed decisions in both directions).
        _dup_rows = len(_prior_spec(_dup).get("rows") or [])
        if len(spec.rows or []) > _dup_rows:
            await materialize_calculation()
            updated = await ctx.canvas_store.patch_section(
                section_id=_dup.id,
                patch={"spec": spec_to_dict(spec)},
                by_turn_id=ctx.turn_id,
            )
            bus = getattr(ctx, "parent_bus", None)
            target = "chat" if getattr(_dup, "surface_target", None) == "chat" else args.get("target")
            suppress_reveal = await _apply_surface_target(ctx, bus, _dup.id, target)
            await _hide_if_early_chat(ctx, bus, ctx.turn_id)
            if not (suppress_reveal or getattr(_dup, "hidden_for_chat_mode", False) or getattr(bus, "_early_chat_surface", False)):
                from bowhead_core.canvas_revise import emit_section_patched

                await emit_section_patched(bus, turn_id=ctx.turn_id, canvas_id=canvas.id, section=updated)
            return {
                "section_id": _dup.id,
                "canvas_id": canvas.id,
                "kind": spec.kind,
                "row_count": spec.row_count,
                "title": spec.title,
                "deduped": "upgraded",
            }
        return {
            "section_id": _dup.id,
            "canvas_id": canvas.id,
            "kind": _prior_spec(_dup).get("kind"),
            "row_count": _dup_rows,
            "title": spec.title,
            "deduped": "skipped_degenerate",
        }

    await materialize_calculation()

    # Different titles, **byte-identical data**: the D6 title criterion above blocks none of these.
    #
    # 116 observations: of 612 turns with ≥2 charts, **77** had identical data + kind but different titles,
    # and **zero** shared titles. Two cases coexist and machines cannot distinguish them (judging title wording is forbidden):
    #   · Redrawing with a different title wastes space;
    #   · A title promising **another cut** while data stays unchanged makes the chart dishonest.
    # t_650f92d9bbb6 demonstrated the latter: category-revenue month-over-month decline TOP20 and growth TOP20
    # used the same artifact/rows, both led by renovation accessories +1308%;
    # the decline ranking plotted growth. Its last-30-day month-over-month change trend also used absolute revenue.
    #
    # Therefore **keep the chart** (dropping harms legitimate retitling and hides the actual problem); return only facts to the model:
    # only it knows this chart's intended meaning (same tradeoff as _series_rank_note).
    _same_data = _prior_chart_with_same_data(_prior_charts, spec, _prior_spec)
    if _same_data is not None:
        result_same_data_note = (
            f"⚠ 本轮已经用**同一份数据**画过《{_same_data}》（逐行一致，不只是同一个 artifact_id）。"
            "如果这张图想表达的是**另一种切法**（同比/环比/衰退榜/占比…），那份数据还没算出来——"
            "先去算，再画；否则就是同一张图画了两次，删掉一张。"
        )
    else:
        result_same_data_note = ""

    if limit_note:
        logger.info("add_chart.limit_ignored_on_time_axis turn=%s limit=%s rows=%s", getattr(ctx, "turn_id", ""), limit_n, len(rows))

    payload: dict[str, Any] = {"spec": spec_to_dict(spec)}
    # #112 API-based chart interleave
    anchor_heading = args.get("anchor_heading")
    if isinstance(anchor_heading, str) and anchor_heading.strip():
        payload["anchor_heading"] = anchor_heading.strip()
    # Batch 2/c10 (PRD §3.11): structured provenance; source workspace persists with the section
    # (generic {tool, context_id, context_name}; names come from gateway-injected snapshots because core has no store access).
    # Legacy/no datarc context → omit field (frontend fallback renders nothing).
    _dws = ""
    _rp = getattr(ctx, "resolve_datarc_project", None)
    if callable(_rp):
        _dws = _rp() or ""
    if _dws:
        payload["data_source"] = {
            "tool": "datarc",
            "context_id": _dws,
            "context_name": getattr(ctx, "active_datarc_workspace_name", "") or "",
        }
    stored = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload=payload,
        by_turn_id=ctx.turn_id,
    )

    # 4. Broadcast SSE live.
    # W12 #93: also emit the P3.5 streaming set (section_streaming + section_done) so frontend uses
    # placeholders and transitions instead of abrupt blocks. Charts have no token chunks,
    # only placeholder→done. Still emit legacy onSectionAdded; dual emission is compatible (frontend handles either
    # path correctly).
    bus = getattr(ctx, "parent_bus", None)
    # Candidate 4: charts with target=chat **emit none** of the revealing events (streaming/done/section_added).
    # Same verdict as candidate 2: do not persist events without delivery; attachCanvasSSE replays Canvas frames
    # on reconnect, making hidden live charts reappear on replay and violating chat invariants. Never emit these frames.
    # Chat-bubble charts are unaffected: inline_artifacts reads the store directly, bypassing these events.
    _suppress_reveal = await _apply_surface_target(ctx, bus, stored.id, args.get("target"))
    if bus is not None and not _suppress_reveal:
        from bowhead_core.canvas_streaming import (
            emit_section_done,
            emit_section_streaming,
        )

        # Emit streaming first (frontend hydrates placeholder).
        with contextlib.suppress(Exception):
            await emit_section_streaming(
                bus,
                turn_id=ctx.turn_id,
                section_id=stored.id,
                kind="chart",
            )
        # Then emit done with real payload (frontend finalizes and transitions out of placeholder).
        with contextlib.suppress(Exception):
            await emit_section_done(
                bus,
                turn_id=ctx.turn_id,
                section_id=stored.id,
                payload=stored.to_report_section(),
            )
        # Preserve legacy section_added compatibility for clients without P3.5 hooks.
        try:
            await bus.emit(
                OrchestratorEvent(
                    event_type="canvas.section_added",  # type: ignore[arg-type]
                    turn_id=ctx.turn_id,
                    payload={
                        "canvas_id": canvas.id,
                        "section": stored.to_report_section(),
                        "seq": stored.seq,
                    },
                )
            )
        except Exception as e:
            logger.warning("add_chart: SSE emit failed: %s", e)
    await _hide_if_early_chat(ctx, getattr(ctx, "parent_bus", None), ctx.turn_id)

    result: dict[str, Any] = {
        "section_id": stored.id,
        "canvas_id": canvas.id,
        "kind": spec.kind,
        "row_count": spec.row_count,
        "title": spec.title,
        "_infer_reason": spec._infer_reason,
    }
    if note_fallbacks:
        result["presentation_fallbacks"] = note_fallbacks
    if spec.views:
        result["views"] = [
            {
                "id": view["id"],
                "title": view["spec"]["title"],
                "kind": view["spec"]["kind"],
                "row_count": view["spec"]["row_count"],
                "drawn_rows": len(view["spec"].get("rows", [])),
                "coverage": {
                    key: view["spec"][key] for key in ("top_n", "series_shown", "series_total", "points_shown", "points_total") if key in view["spec"]
                },
            }
            for view in spec.views
        ]
    if calculation_metadata:
        result["derived_columns"] = calculation_metadata
        result["calculation_missing"] = calculation_missing or {}
    # Models base body text on this: disclose truncation, selected N items and total, never describe top N as all items.
    if spec.top_n and spec.row_count > spec.top_n:
        result["drawn_rows"] = spec.top_n
        result["note"] = f"图上只画了 {spec.top_n} / {spec.row_count} 项（图下已注明），正文别说成全部。"
    # Series truncation (chart/infer.py _cap_time_series) is a **separate criterion** with empty top_n,
    # invisible to the preceding check. Omitting it makes models describe a 15-province chart as all provinces' trends,
    # erasing 44 provinces (add_chart results are the model's sole basis for body text).
    sampled = downsampled_points(spec)
    if sampled is not None:
        pts_shown, pts_total = sampled
        result["drawn_points"] = pts_shown
        result["points_total"] = pts_total
        pts_note = (
            f"时间点太多（{pts_total} 个），图上按整体走势降采样到 {pts_shown} 个（首尾时刻保留，"
            "整段时间没缺）。正文可以照常讲走势，但别把某一天的值说成「图上每天都有」，"
            "也别据此断言「期间无异常」——挑点看的是各系列合计，量级小的那条的尖峰可能没被画出来。"
        )
        result["note"] = f"{result.get('note', '')} {pts_note}".strip()
    capped = capped_series(spec)
    if capped is not None:
        shown_series, total_series = capped
        result["drawn_series"] = shown_series
        result["series_total"] = total_series
        series_note = f"图上只画了合计最大的 {shown_series} / {total_series} 条系列（图下已注明），正文别说成全部；要全部就先在取数侧聚合。"
        # ★ Return **rankings** too, not just a count.
        #
        # 116 dogfood t_f4edf5b877b3: total-based gate correctly selected top 15 (women's wear first at 6.249 billion),
        # but the model's only named data was bi_query's first ten inline rows (January, category-name pinyin order).
        # Thermal underwear and kitchenware led those ten, so its final answer called them the absolute sales pillars,
        # while women's wear was an order of magnitude larger in the chart on the same screen and absent from body text.
        # The returned warning **already** prohibited conclusions from those ten rows; the model did exactly that.
        # Merely forbidding it is insufficient: **show the correct answer** (same lesson as _available_data_hint).
        series_note += _series_rank_note(spec)
        result["note"] = f"{result.get('note', '')} {series_note}".strip()
    if limit_note:
        result["note"] = f"{result.get('note', '')} {limit_note}".strip()
    if result_same_data_note:
        result["same_data_as"] = _same_data
        result["note"] = f"{result.get('note', '')} {result_same_data_note}".strip()
    # Missing chart points still appear in the model's computed-table prose; disclose which cells were not plotted.
    if _non_finite:
        _nf_cols = "、".join(f"{c}（{n} 格）" for c, n in _non_finite.items())
        _nf_note = (
            f"数据里有无穷大/非数值的单元格：{_nf_cols}，多半是基数为 0 的增长率。"
            "图上按空值处理、没有画出来；正文别把这些项说成增长最快或排第一，要讲就说明基数为 0。"
        )
        result["note"] = f"{result.get('note', '')} {_nf_note}".strip()
    # Warn when rows have valid numbers but no distinguishing labels, allowing the
    # Agent to correct column selection. This guard does not restore lost dimensions;
    # _lead_dim_col and table projection handle those separately. It also covers data
    # that genuinely has no distinguishing column and is exclusive with the single-
    # row fallback below.
    if (_indistinct := indistinguishable_rows_reason(rows, spec.columns or [])) is not None:
        result["warning"] = "rows_are_indistinguishable"
        result["hint"] = (
            f"这张图/表{_indistinct}——读的人无法知道哪一行对应哪个对象。"
            "先确认 artifact 里到底有没有一列能区分这些行（大区/品牌/月份…）："
            "有就把它显式写进 `column_types` 并用同一 title 重画；"
            "真的没有，就说明上游 groupby 把分组维度聚掉了，回去重取。"
        )
    if _chart_request_needs_series(args) and len(rows) < 2:
        result["warning"] = "series_requested_but_single_row"
        result["hint"] = (
            f"标题/意图要求走势序列, 但 {aid} 只有 {len(rows)} 行(已降级为 KPI 卡)。"
            "请检查 data_artifact_id 是否传错——用包含逐月/逐日多行数据的那个 "
            "artifact 以同一 title 重新调用本工具, 会自动升级原图。"
        )
    await _attach_prior_charts_reminder(result, ctx, canvas.id)
    return result


async def _attach_prior_charts_reminder(result: dict[str, Any], ctx: ToolExecutionContext, canvas_id: str) -> None:
    """canvas-update-turn 批 B2-2：修订档画完新图时，提醒上一轮哪些图还在——画图那一刻最知道新图取代了谁。

    只在 Lead 档位是 revise 时挂（append 是加一节，旧图照理都该留着）。best-effort：取不到就不挂。"""
    from bowhead_core.canvas_revise import lead_canvas_mode, prior_charts_reminder, prior_visible_charts, section_revise_enabled

    if not section_revise_enabled() or lead_canvas_mode(getattr(ctx, "parent_bus", None)) != "revise":
        return
    with contextlib.suppress(Exception):
        prior = prior_visible_charts(await ctx.canvas_store.list_sections(canvas_id, include_hidden=True), this_turn_id=ctx.turn_id)
        if prior:
            result["prior_charts"] = prior
            result["note"] = f"{result.get('note', '')} {prior_charts_reminder(prior)}".strip()


# ─────────────────────────────────────────────────────────────
# remove_chart (canvas-update-turn batch B2-2)
# ─────────────────────────────────────────────────────────────
def _remove_chart_schema() -> ToolSchema:
    return ToolSchema(
        name="remove_chart",
        description=(
            "收起画布上**上一轮**的图表（图、图片、数据表都算）。两种情况用它：\n"
            '- 你刚用 add_chart 画的新图取代了上一轮的某张图（同一主题换了口径/范围/只留前 N）→ reason="replaced"；\n'
            '- 用户要求去掉某张图 → reason="removed"。\n'
            "收起不是删除：图进报告末尾的「已收起的图表」，用户展开可以查看。只收你确定不再需要的；没被取代的旧图不要动。"
            "正文里还引用着它（`[figure:id]`）的图会被拒——先用 write_report 把那处锚改成新图或删掉。"
            "id 用 add_chart 返回的 prior_charts 或 read_report 的 figures 里的真实 id，**绝不编造**。"
            "本轮刚画的图不用收（画错了就重画）。"
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["section_ids", "reason"],
            "properties": {
                "section_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
                "reason": {"type": "string", "enum": ["replaced", "removed"]},
            },
        },
    )


async def _remove_chart(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    # Can fold any prior-turn Canvas chart, without per-section ownership; same trust level as canvas_patch_section:
    # system manifests of write trust class only (REPORT_WRITE_TOOLS fences custom agents out of authorship; this is the second runtime gate).
    from bowhead_core.agents._write_guard import check_write_trust_class
    from bowhead_core.canvas_revise import REMOVE_REASONS, remove_charts
    from bowhead_core.resource_disable import team_agent_opened_tools

    # Opened to team Agents: an Agent may hide charts in the user's
    # own session just as the user can (user decision 2026-10-08).
    if "remove_chart" not in team_agent_opened_tools() and (i3_err := check_write_trust_class()) is not None:
        return i3_err
    ids = args.get("section_ids")
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list) or not ids:
        return {"error": "section_ids 必填（上一轮图表的真实 id 列表）"}
    reason = str(args.get("reason") or "").strip().lower()
    if reason not in REMOVE_REASONS:
        return {"error": "reason 只能是 replaced（被新图取代）或 removed（用户要求去掉）"}
    store = getattr(ctx, "canvas_store", None)
    session_id = getattr(ctx, "session_id", None)
    canvas = await store.get_canvas_by_session(session_id) if store is not None and session_id else None
    if canvas is None:
        return {"error": "当前会话还没有画布，没有可收起的图"}
    return await remove_charts(
        store, getattr(ctx, "parent_bus", None), canvas_id=canvas.id, turn_id=getattr(ctx, "turn_id", None), section_ids=ids, reason=reason
    )


# ─────────────────────────────────────────────────────────────
# add_image（multimodal-v1）
# ─────────────────────────────────────────────────────────────
# caption/alt are LLM prose; strip citation/inline markers (dual-path rule:
# [ref:]/[num:] belong only to Canvas rendering, [cite:]/[data:] only to chat; captions are
# terminal display text and must leak neither). Match render_deck.py _MARK_CITE pattern.
_CAPTION_MARKER_RE = re.compile(r"\[(?:ref|cite|chart|num|data):[^\]]*\]|\[\d+\]", re.IGNORECASE)
# Inline raster allowlist (same set as gateway files.py inline disposition).
_IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp")


def _strip_caption_markers(s: str) -> str:
    return _CAPTION_MARKER_RE.sub("", s or "").strip()


def _add_image_schema() -> ToolSchema:
    return ToolSchema(
        name="add_image",
        description=(
            "把一张**已生成的 PNG/JPEG 图片**（artifact_id）加到画布作为图片区块. 两个来源: "
            "①`image_generate` 返的海报/宣传图 artifact_id；"
            "②`python_sandbox` 出的 PNG——**仅限原生图表画不了的类型**: 地图 / 箱线图·小提琴图·"
            "QQ plot 等统计诊断图 / 多子图矩阵(facet grid) / treemap·雷达·甘特·网络图. "
            "⚠ **散点例外**: 用户要「每个点标上名字」时仍走 `add_chart` —— "
            "原生散点会自动挑互不重叠的一批点常驻标注(导出/触屏必标), 其余 hover 可见, "
            "**别为了标名字去 savefig**. "
            "⚠ **趋势·对比·占比·排名·结构·贡献分解(瀑布)·流向(桑基)·相关 → 一律 `add_chart`** "
            "(sandbox 里 `df.to_csv('out.csv')` 出数据喂它, **别 `savefig`**) —— PNG 会丢掉底层数据、"
            "用户切换图表视角的能力和数字溯源. "
            "兜底: 图**已经** savefig 出来了 → 仍挂这里, 别放弃不挂(那样 payload 说有图实际没图)."
        ),
        input_schema={
            "type": "object",
            "required": ["artifact_id"],
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["chat", "canvas"],
                    "description": (
                        "这张图去哪：`chat`=只进聊天气泡、不上画布；`canvas`=进画布。"
                        "**不确定就别传**——不传时沿用本轮的交付面判定，那是默认且正确的行为。"
                        "典型用途：报告轮里画了一张只为核对的中间图，传 `chat` 让它不污染报告。"
                        "⚠ 只能收窄：本轮已确定是聊天答复时，传 `canvas` 会被忽略。"
                    ),
                },
                "artifact_id": {
                    "type": "string",
                    "description": (
                        "`image_generate` 返的图片 artifact_id, 或 `python_sandbox` 输出的 PNG "
                        "artifact_id (art_xxx) —— 后者仅限原生图表画不了的类型, 见 tool description."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": "（可选）图下说明文字（≤200 字），如海报文案概述.",
                },
                "alt": {
                    "type": "string",
                    "description": "（可选）无障碍替代文本（≤120 字），描述画面内容.",
                },
                "anchor_heading": {
                    "type": "string",
                    # Batch B2-4: like add_chart, ordering was never wired; make the promise truthful.
                    "description": (
                        "（可选）只作标记，**不会**移动图的位置。要让图出现在某个论点旁边，"
                        "在 write_report 的正文里该论点之后独占一行写 `[figure:<本工具返回的 section_id>]`。"
                    ),
                },
            },
        },
    )


async def _add_image(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}

    aid = str(args.get("artifact_id") or "").strip()
    if not aid:
        return {"error": "artifact_id required (use image_generate first)"}
    if not _VALID_DATA_ARTIFACT_ID_RE.match(aid):
        # Same silent-strip as chart: success shape + skipped reason; LLM self-corrects next turn.
        return {
            "skipped": "invalid_artifact_id_format",
            "artifact_id": aid,
            "hint": (f"artifact_id {aid!r} 不是合法格式 (expected art_xxxxxxxxxxxx). 请先调 image_generate 拿真实图片 artifact_id."),
        }
    if ctx.artifact_store is None:
        return {"error": "artifact_store not configured on ctx"}
    if ctx.canvas_store is None or not ctx.session_id:
        return {"error": "canvas_store/session_id not configured on ctx"}

    # Owner check (same #54 rule: prevent unauthorized artifact IDs recalled from another user).
    art = await ctx.artifact_store.get(aid, check_created_by=ctx.user_id)
    if art is None:
        return {"error": f"artifact {aid!r} not found"}
    mime = (getattr(art, "mime", "") or "").lower()
    if mime not in _IMAGE_MIMES:
        return {
            "skipped": "not_an_image",
            "artifact_id": aid,
            "hint": (f"artifact {aid!r} 的 mime={mime!r} 不是图片 (允许 {', '.join(_IMAGE_MIMES)}). 数据类 artifact 走 add_chart."),
        }

    caption = _strip_caption_markers(str(args.get("caption") or ""))[:200]
    alt = _strip_caption_markers(str(args.get("alt") or ""))[:120]
    payload: dict[str, Any] = {"artifact_id": aid}
    if caption:
        payload["caption"] = caption
    if alt:
        payload["alt"] = alt
    anchor_heading = args.get("anchor_heading")
    if isinstance(anchor_heading, str) and anchor_heading.strip():
        payload["anchor_heading"] = anchor_heading.strip()

    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    stored = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="image",
        payload=payload,
        by_turn_id=ctx.turn_id,
    )

    # Same SSE trio as chart: streaming placeholder → done finalization → legacy section_added.
    bus = getattr(ctx, "parent_bus", None)
    # Candidate 4: exactly like chart; these are the only two visual tools whose figures can inline into chat bubbles.
    _suppress_reveal = await _apply_surface_target(ctx, bus, stored.id, args.get("target"))
    if bus is not None and not _suppress_reveal:
        from bowhead_core.canvas_streaming import (
            emit_section_done,
            emit_section_streaming,
        )

        with contextlib.suppress(Exception):
            await emit_section_streaming(
                bus,
                turn_id=ctx.turn_id,
                section_id=stored.id,
                kind="image",
            )
        with contextlib.suppress(Exception):
            await emit_section_done(
                bus,
                turn_id=ctx.turn_id,
                section_id=stored.id,
                payload=stored.to_report_section(),
            )
        try:
            await bus.emit(
                OrchestratorEvent(
                    event_type="canvas.section_added",  # type: ignore[arg-type]
                    turn_id=ctx.turn_id,
                    payload={
                        "canvas_id": canvas.id,
                        "section": stored.to_report_section(),
                        "seq": stored.seq,
                    },
                )
            )
        except Exception as e:
            logger.warning("add_image: SSE emit failed: %s", e)
    await _hide_if_early_chat(ctx, getattr(ctx, "parent_bus", None), ctx.turn_id)

    return {
        "section_id": stored.id,
        "canvas_id": canvas.id,
        "kind": "image",
        "artifact_id": aid,
    }


# ─────────────────────────────────────────────────────────────
# canvas_create_design / canvas_update_design_text（multimodal-v2A）
# ─────────────────────────────────────────────────────────────
def _create_design_schema() -> ToolSchema:
    return ToolSchema(
        name="canvas_create_design",
        description=(
            "创建可编辑设计稿：背景图片 artifact + 多个 text layers。用于海报/banner/"
            "促销图等需要后续精确改字的物料。先用 image_generate(purpose=design_background) "
            "生成背景，再把必要文案放入 layers。"
        ),
        input_schema={
            "type": "object",
            "required": ["background_artifact_id", "title", "size_preset", "layers"],
            "properties": {
                "background_artifact_id": {
                    "type": "string",
                    "description": "image_generate(purpose=design_background) 返回的图片 artifact_id。",
                },
                "title": {"type": "string", "description": "设计稿标题（≤60 字）。"},
                "size_preset": {
                    "type": "string",
                    "enum": list(DESIGN_SIZE_PRESETS),
                    "description": "画幅预设。",
                },
                "layers": {
                    "type": "array",
                    "description": "text layer 数组。V2A 只支持 type=text。",
                },
            },
        },
    )


def _update_design_text_schema() -> ToolSchema:
    return ToolSchema(
        name="canvas_update_design_text",
        description=(
            "确定性修改 design section 里的一个 text layer。优先用 layer_id；没有 layer_id "
            "时用 match_text 精确匹配当前文字。不会调用图片模型，不接受 partial layers。"
        ),
        input_schema={
            "type": "object",
            "required": ["section_id", "text"],
            "anyOf": [
                {"required": ["layer_id"]},
                {"required": ["match_text"]},
            ],
            "properties": {
                "section_id": {"type": "string"},
                "layer_id": {"type": "string"},
                "match_text": {"type": "string"},
                "text": {"type": "string"},
                "expected_rev": {"type": "integer"},
            },
        },
    )


def _flatten_design_schema() -> ToolSchema:
    return ToolSchema(
        name="canvas_flatten_design",
        description=("把 design section flatten 成可下载 PNG artifact。用于用户明确要求下载/导出当前可编辑海报时调用。"),
        input_schema={
            "type": "object",
            "required": ["section_id"],
            "properties": {
                "section_id": {
                    "type": "string",
                    "description": "要导出的 design section id。",
                },
                "expected_rev": {
                    "type": "integer",
                    "description": "可选 revision guard；冲突时返回 current_rev。",
                },
            },
        },
    )


async def _emit_design_added(ctx: ToolExecutionContext, canvas_id: str, stored: Any) -> None:
    bus = getattr(ctx, "parent_bus", None)
    if bus is None:
        return
    from bowhead_core.canvas_streaming import (
        emit_section_done,
        emit_section_streaming,
    )

    with contextlib.suppress(Exception):
        await emit_section_streaming(
            bus,
            turn_id=ctx.turn_id,
            section_id=stored.id,
            kind="design",
        )
    with contextlib.suppress(Exception):
        await emit_section_done(
            bus,
            turn_id=ctx.turn_id,
            section_id=stored.id,
            payload=stored.to_report_section(),
        )
    try:
        await bus.emit(
            OrchestratorEvent(
                event_type="canvas.section_added",  # type: ignore[arg-type]
                turn_id=ctx.turn_id,
                payload={
                    "canvas_id": canvas_id,
                    "section": stored.to_report_section(),
                    "seq": stored.seq,
                },
            )
        )
    except Exception as e:
        logger.warning("canvas_create_design: SSE emit failed: %s", e)


async def _emit_design_patched(
    ctx: ToolExecutionContext,
    *,
    canvas_id: str,
    section_id: str,
    patch: dict[str, Any],
    rev: int,
    source: str,
) -> None:
    bus = getattr(ctx, "parent_bus", None)
    if bus is None:
        return
    try:
        await bus.emit(
            OrchestratorEvent(
                event_type="canvas.section_patched",  # type: ignore[arg-type]
                turn_id=ctx.turn_id,
                payload={
                    "canvas_id": canvas_id,
                    "section_id": section_id,
                    "kind": "design",
                    "patch": patch,
                    "rev": rev,
                    "ok": True,
                },
            )
        )
    except Exception as e:
        logger.warning("%s: SSE patch emit failed: %s", source, e)


async def _create_design(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}
    if not is_design_v2a_enabled():
        _metric_inc("bowhead_design_create_total", {"status": "disabled", "size_preset": "unknown"})
        return {"skipped": "design_v2a_disabled"}
    if ctx.artifact_store is None:
        _metric_inc("bowhead_design_create_total", {"status": "error", "size_preset": "unknown"})
        return {"error": "artifact_store not configured on ctx"}
    if ctx.canvas_store is None or not ctx.session_id:
        _metric_inc("bowhead_design_create_total", {"status": "error", "size_preset": "unknown"})
        return {"error": "canvas_store/session_id not configured on ctx"}

    aid = str(args.get("background_artifact_id") or "").strip()
    size_preset = str(args.get("size_preset") or "portrait_3_4")
    size_metric = _design_size_metric_label(size_preset)
    if not _VALID_DATA_ARTIFACT_ID_RE.match(aid):
        _metric_inc("bowhead_design_create_total", {"status": "invalid_artifact", "size_preset": size_metric})
        return {
            "skipped": "invalid_artifact_id_format",
            "background_artifact_id": aid,
            "hint": "请先调 image_generate(purpose=design_background) 拿真实图片 artifact_id.",
        }
    art = await ctx.artifact_store.get(aid, check_created_by=ctx.user_id)
    if art is None:
        _metric_inc("bowhead_design_create_total", {"status": "invalid_artifact", "size_preset": size_metric})
        return {"error": f"artifact {aid!r} not found"}
    mime = (getattr(art, "mime", "") or "").lower()
    if mime not in _IMAGE_MIMES:
        _metric_inc("bowhead_design_create_total", {"status": "invalid_artifact", "size_preset": size_metric})
        return {
            "skipped": "not_an_image",
            "background_artifact_id": aid,
            "hint": f"artifact {aid!r} 的 mime={mime!r} 不是图片.",
        }

    payload, layer_error = normalize_design_payload(
        title=args.get("title") or "可编辑设计稿",
        size_preset=size_preset,
        background_artifact_id=aid,
        layers=args.get("layers"),
    )
    if layer_error or payload is None:
        _metric_inc("bowhead_design_create_total", {"status": "invalid_layer", "size_preset": size_metric})
        return {"error": f"invalid design layers: {layer_error}"}
    size_preset = str(payload["size_preset"])
    size_metric = _design_size_metric_label(size_preset)
    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    stored = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="design",
        payload=payload,
        by_turn_id=ctx.turn_id,
    )
    await _emit_design_added(ctx, canvas.id, stored)
    _metric_inc("bowhead_design_create_total", {"status": "created", "size_preset": size_metric})
    return {
        "section_id": stored.id,
        "canvas_id": canvas.id,
        "kind": "design",
        "background_artifact_id": aid,
        "rev": stored.rev,
    }


def _translate_simple_section_ref(ctx: ToolExecutionContext, raw: str) -> str:
    """Translate a ν1.5 simple-int section ref → real id (align with
    add_chart). Real ids / unknown values pass through unchanged."""
    if not raw or not _SIMPLE_INT_REF_RE.match(raw):
        return raw
    allowlist = run_ref_allowlist(ctx)
    if allowlist is None:
        return raw
    try:
        resolved = allowlist.resolve(raw)
    except Exception:
        return raw
    return resolved if isinstance(resolved, str) and resolved else raw


async def _resolve_target_design_section(
    ctx: ToolExecutionContext,
    raw_section_id: str,
) -> tuple[Any, Any, dict[str, Any] | None]:
    """Robustly resolve the design section an LLM edit/flatten tool should act on.

    Cross-turn subagents only see ν1.5 simple-int refs and design sections are
    not surfaced in ``<available_refs>``, so the model frequently passes a wrong
    ``section_id`` (a ``canvas_id``, a chart simple-int, or a fabricated
    ``sec_N``) and the precise-match tools used to fail → the model gave up and
    regenerated the whole poster. This resolver makes the tools self-sufficient:

      1. simple-int → real id translation (same allowlist as add_chart);
      2. exact match against the canvas' design sections;
      3. single-design fallback — the dominant "one poster" case;
      4. otherwise needs_clarification listing the real design section ids.

    Returns ``(canvas, section, None)`` on success, else ``(canvas|None, None,
    clarify_dict)`` where ``clarify_dict`` is the tool result to surface.
    """
    if ctx.canvas_store is None or not ctx.session_id:
        return None, None, {"error": "canvas_store/session_id not configured on ctx"}
    canvas = await ctx.canvas_store.get_canvas_by_session(ctx.session_id)
    if canvas is None:
        return None, None, {"error": "canvas not found"}
    sections = await ctx.canvas_store.list_sections(canvas.id, include_hidden=True)
    designs = [s for s in sections if s.kind == "design"]
    want = _translate_simple_section_ref(ctx, str(raw_section_id or "").strip())
    for section in designs:
        if section.id == want:
            return canvas, section, None
    if len(designs) == 1:
        logger.info(
            "design.section_resolve.single_fallback raw=%r resolved=%r -> %s",
            raw_section_id,
            want,
            designs[0].id,
        )
        return canvas, designs[0], None
    if not designs:
        return (
            canvas,
            None,
            {
                "skipped": "no_design_section",
                "section_id": raw_section_id,
                "hint": "当前 canvas 没有可编辑设计稿; 先用 canvas_create_design 创建.",
            },
        )
    return (
        canvas,
        None,
        {
            "skipped": "needs_clarification",
            "reason": "design_section_ambiguous",
            "section_id": raw_section_id,
            "designs": [
                {
                    "section_id": s.id,
                    "title": str(s.payload.get("title") or ""),
                    "text_layers": design_text_layer_summary(s.payload),
                }
                for s in designs
            ],
            "hint": "canvas 内有多个设计稿; 用上面列出的 section_id 指定要操作的那一个.",
        },
    )


async def _update_design_text(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}
    if not is_design_v2a_enabled():
        _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "disabled"})
        return {"skipped": "design_v2a_disabled"}
    if ctx.canvas_store is None or not ctx.session_id:
        _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "error"})
        return {"error": "canvas_store/session_id not configured on ctx"}

    section_id = str(args.get("section_id") or "").strip()
    if not section_id:
        _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "error"})
        return {"error": "section_id required"}
    layer_id = str(args.get("layer_id") or "").strip()
    match_text = clean_design_text(args.get("match_text"), max_chars=160)
    if not layer_id and not match_text:
        _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "missing_locator"})
        return {"skipped": "missing_layer_locator", "section_id": section_id}
    text = clean_design_text(args.get("text"), max_chars=160)
    if not text:
        _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "empty_text"})
        return {
            "skipped": "empty_text",
            "section_id": section_id,
            "reason": "text_required",
        }
    canvas, section, clarify = await _resolve_target_design_section(ctx, section_id)
    if clarify is not None:
        _metric_inc(
            "bowhead_design_patch_total",
            {"field": "text", "status": str(clarify.get("skipped") or "not_found")},
        )
        return clarify
    # Resolver may have redirected a wrong/simple-int id to the real design section.
    section_id = section.id

    layers = section.payload.get("layers")
    if not isinstance(layers, list):
        _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "error"})
        return {"error": "invalid design payload: layers missing"}
    candidates: list[int] = []
    if layer_id:
        candidates = [
            idx
            for idx, layer in enumerate(layers)
            if isinstance(layer, dict) and layer.get("type") == "text" and str(layer.get("id") or "") == layer_id
        ]
        if not candidates:
            _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "needs_clarification"})
            return {
                "skipped": "needs_clarification",
                "section_id": section_id,
                "reason": "layer_id_not_found",
                "text_layers": design_text_layer_summary(section.payload),
            }
    else:
        candidates = [
            idx
            for idx, layer in enumerate(layers)
            if isinstance(layer, dict) and layer.get("type") == "text" and str(layer.get("text") or "") == match_text
        ]
        if not candidates:
            _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "needs_clarification"})
            return {
                "skipped": "needs_clarification",
                "section_id": section_id,
                "reason": "match_text_not_found",
                "text_layers": design_text_layer_summary(section.payload),
            }
        if len(candidates) > 1:
            _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "ambiguous_layer"})
            return {
                "skipped": "ambiguous_layer",
                "section_id": section_id,
                "candidate_layer_ids": [str(layers[idx].get("id") or "") for idx in candidates if isinstance(layers[idx], dict)],
            }

    next_layers = [dict(layer) if isinstance(layer, dict) else layer for layer in layers]
    target_idx = candidates[0]
    target = dict(next_layers[target_idx])
    target["text"] = text
    next_layers[target_idx] = target

    expected_rev_raw = args.get("expected_rev")
    try:
        expected_rev = int(expected_rev_raw) if expected_rev_raw is not None else None
    except (TypeError, ValueError):
        expected_rev = None
    patch = {
        "layers": next_layers,
        "flattened_artifact_id": None,
        "flattened_source_rev": None,
        "flattened_at": None,
    }
    try:
        updated = await ctx.canvas_store.patch_section(
            section_id=section.id,
            patch=patch,
            expected_rev=expected_rev,
            by_turn_id=ctx.turn_id,
        )
    except CanvasRevConflictError as exc:
        _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "rev_conflict"})
        return {
            "skipped": "rev_conflict",
            "section_id": section_id,
            "expected_rev": expected_rev,
            "current_rev": exc.current_rev,
        }
    if updated is None:
        _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "not_found"})
        return {"error": f"design section {section_id!r} not found"}
    await _emit_design_patched(
        ctx,
        canvas_id=canvas.id,
        section_id=updated.id,
        patch=patch,
        rev=updated.rev,
        source="canvas_update_design_text",
    )
    _metric_inc("bowhead_design_patch_total", {"field": "text", "status": "done"})
    return {
        "section_id": updated.id,
        "layer_id": str(target.get("id") or ""),
        "rev": updated.rev,
        "text": text,
    }


async def _flatten_design(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}
    if not is_design_v2a_enabled():
        _metric_inc("bowhead_design_flatten_total", {"status": "disabled", "backend": "unknown"})
        return {"error": "design_v2a_disabled", "message": "editable design is disabled"}
    if ctx.artifact_store is None:
        _metric_inc("bowhead_design_flatten_total", {"status": "error", "backend": "unknown"})
        return {"error": "artifact_store not configured on ctx"}
    if ctx.canvas_store is None or not ctx.session_id:
        _metric_inc("bowhead_design_flatten_total", {"status": "error", "backend": "unknown"})
        return {"error": "canvas_store/session_id not configured on ctx"}

    section_id = str(args.get("section_id") or "").strip()
    if not section_id:
        _metric_inc("bowhead_design_flatten_total", {"status": "error", "backend": "unknown"})
        return {"error": "section_id required"}
    expected_rev_raw = args.get("expected_rev")
    try:
        expected_rev = int(expected_rev_raw) if expected_rev_raw is not None else None
    except (TypeError, ValueError):
        expected_rev = None
    canvas, section, clarify = await _resolve_target_design_section(ctx, section_id)
    if clarify is not None:
        _metric_inc(
            "bowhead_design_flatten_total",
            {"status": str(clarify.get("skipped") or "not_found"), "backend": "unknown"},
        )
        return clarify
    # Resolver may have redirected a wrong/simple-int id to the real design section.
    section_id = section.id
    try:
        from bowhead_core.design_renderer import (
            DesignFlattenError,
            flatten_design_section,
        )

        result = await flatten_design_section(
            artifact_store=ctx.artifact_store,
            canvas_store=ctx.canvas_store,
            canvas_id=canvas.id,
            section_id=section_id,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            expected_rev=expected_rev,
            created_by_turn=ctx.turn_id,
        )
    except DesignFlattenError as exc:
        out: dict[str, Any] = {
            "error": exc.code,
            "message": exc.message,
            "section_id": section_id,
        }
        if exc.current_rev is not None:
            out["current_rev"] = exc.current_rev
        return out
    patch = {
        "flattened_artifact_id": result.artifact_id,
        "flattened_source_rev": result.section.get("flattened_source_rev"),
        "flattened_at": result.section.get("flattened_at"),
    }
    await _emit_design_patched(
        ctx,
        canvas_id=canvas.id,
        section_id=section_id,
        patch=patch,
        rev=result.rev,
        source="canvas_flatten_design",
    )
    return {
        "artifact_id": result.artifact_id,
        "url": f"/api/v1/files/{result.artifact_id}",
        "section_id": section_id,
        "rev": result.rev,
        "warnings": result.warnings,
    }


#: Bare 12-digit hex artifact ID, missing only the art_ prefix for validity.
_BARE_ARTIFACT_HEX_RE = re.compile(r"^[0-9a-f]{12}$")


class SeriesCut(NamedTuple):
    """系列被截了：画了几条、一共几条、留下的那几条按合计从大到小是谁。

    `rank` 两条截断路径都要给——只给数字不给名字，模型就会照内联的头几行推断谁是头部
    （116 `t_f4edf5b877b3` 实测）。两条路一条给一条不给，等于把 bug 留在另一半场景上。
    """

    shown: int
    total: int
    rank: list[tuple[str, float]]


#: Number of named ranking entries in notes: sufficient to identify leaders without bloating note text.
_SERIES_RANK_NAMED = 5


def _prior_chart_with_same_data(prior: list[Any], spec: Any, spec_of: Any) -> str | None:
    """本轮是否已有一张**数据逐行相同、标题不同**的图；有就返回它的标题。

    比的是**画出来的行**，不是 `data_artifact_id`：同一个 artifact 配不同的
    `series` / `limit` 会画出真的不同的图（那是合法的），而不同 artifact 也可能
    装着同一份数据（沙箱算两遍）。判据必须落在最终进图的那批行上。
    """
    try:
        mine = json.dumps(spec.rows or [], sort_keys=True, ensure_ascii=False)
    except Exception:  # noqa: S110
        return None
    for sec in prior:
        ps = spec_of(sec)
        if str(ps.get("title") or "") == str(spec.title or ""):
            # D6 handles same titles (upgrade or skip). ★ This line is **currently unreachable**: D6 hits return
            # directly before this point (observed to survive mutation testing). Retain it because it assumes
            # the earlier code returns, something this function cannot guarantee. Removing it and later changing D6
            # not to return would send same-title charts a confusing already-drawn-same-title warning.
            continue
        try:
            if json.dumps(ps.get("rows") or [], sort_keys=True, ensure_ascii=False) == mine:
                return str(ps.get("title") or "")
        except Exception:  # noqa: S112
            continue
    return None


def _series_rank_note(spec: Any) -> str:
    """「按合计从大到小是：女装 62.5亿、鞋包皮件 24.9亿、…」

    排名是 `_cap_time_series` 挑前 N 时的副产物（挂在 `spec._series_rank` 上，
    不是 dataclass 字段 ⇒ 不落库、不进前端）。取不到就不说，绝不因为一句提示让 add_chart 失败。
    """
    rank = getattr(spec, "_series_rank", None)
    if not rank:
        return ""
    # Reuse the writer's number formatting: same model-readable numbers, no second implementation.
    # It deliberately uses significant digits for |v|<1; fixed two decimals would turn 0.0064 into 0.01.
    # Local import avoids the report_writer_loop → canvas_tools cycle.
    from bowhead_core.report_writer_loop import _fmt_num

    try:
        named = [f"{label}（{_fmt_num(value)}）" for label, value in rank[:_SERIES_RANK_NAMED]]
    except Exception:  # noqa: S110
        return ""
    if not named:
        return ""
    more = "" if len(rank) <= _SERIES_RANK_NAMED else f"，其余 {len(rank) - _SERIES_RANK_NAMED} 条依次递减"
    return f" 按合计从大到小依次是：{'、'.join(named)}{more}。说「头部/最大/支柱」只能用这个排名，别照内联的头几行推断。"


def _available_data_hint(ctx: ToolExecutionContext) -> str:
    """「本轮可选的数据 id 是这些」——拒绝提示里最有用的那半句。

    116 实测 346 次被拒里 **230 次（63%）**给的号根本不在 allowlist 里（模型自己编的），
    另有 81 次给的是 `code` artifact 的号。两种都不是「翻译」能救的：翻译器早就接上了
    （见 `resolve_simple_data_ref`），可它没东西可翻。模型缺的是**正确答案长什么样**。
    再看一组：346 次被拒里 **336 次**发生在本轮此前**已经产出过** data artifact 之后——
    正确答案就在手边，只是它看不见。

    ⚠ 清单为空时**不能**断言「本轮没有数据」：allowlist 每圈 iteration 开头才
    refresh，同一圈里刚 `bi_query` 出来的那份还没进去。那种情况下「用上一条
    `bi_query` 返的 `_ref_artifact_id`」才是真话（[[feedback_comment_states_intent_not_outcome]]）。
    """
    allowlist = run_ref_allowlist(ctx)
    if allowlist is None:
        return ""
    try:
        choices = allowlist.choices_of_kind("data")
    except Exception:  # noqa: S110
        return ""
    if not choices:
        return " 清单里暂时没有可用的 data id：如果你刚调过 `bi_query`，直接用它返回的 `_ref_artifact_id`（`art_` 开头那串）；还没取过数就先调 `bi_query`."
    rendered = "；".join(f"{sid} 或 {real_id}" + (f"（{label}）" if label else "") for sid, real_id, label in choices)
    return f" 本轮可用的数据是：{rendered}. 从里面挑一个传进来，或重新 `bi_query` 取新数据."


def resolve_simple_data_ref(
    aid: str,
    ctx: ToolExecutionContext,
) -> tuple[str, str | None]:
    """把 ν1.5 的**简单整数 ref** 翻成真 artifact id。返 `(id, kind)`。

    简单整数在模型面是合法引用（`<available_refs>` 里就是这么给的），但要拿去取文件
    的地方最终需要真 artifact id。`kind == "data"` 才放行 —— 例如 `"12"` 若是
    「画布上第 12 张图」，它是 `sec_*`、不是文件，放行就会取不到。

    ⚠ **消费者不止一个**（`add_chart` 的 `data_artifact_id`、沙箱入口的 `inputs`），
    所以名字是中性的、别再写成 `_for_chart`。判据只能有这一份：再抄一份出去，两边
    必然漂移（[[feedback_one_concept_many_implementations]]）。
    解析不出时**原样返回**，由调用方决定怎么报错。
    """
    if not _SIMPLE_INT_REF_RE.match(aid):
        return aid, None
    allowlist = run_ref_allowlist(ctx)
    if allowlist is None:
        return aid, None
    try:
        simple_id = int(aid)
        kind = allowlist.kind_of(simple_id)
    except Exception:
        return aid, None
    if kind != "data":
        return aid, kind
    try:
        resolved = allowlist.resolve(aid)
    except Exception:
        return aid, kind
    if isinstance(resolved, str) and _VALID_DATA_ARTIFACT_ID_RE.match(resolved):
        logger.info(
            "simple_data_ref_resolved turn=%s simple=%s artifact=%s",
            getattr(ctx, "turn_id", ""),
            aid,
            resolved,
        )
        return resolved, kind
    return aid, kind


# ─────────────────────────────────────────────────────────────
# canvas_add_insight (M157 paragraph-style insight output)
# ─────────────────────────────────────────────────────────────
def _current_invocations(ctx: Any) -> list[Any]:
    """本 peer 可见的 tool invocations——ContextVar 优先（task-local, 并行
    sibling 隔离, impl-review B-P2-9），共享 ctx attr 兜底（老 harness）。"""
    try:
        from bowhead_core.agents._lead_context import (
            get_current_subagent_invocations,
        )

        cv = get_current_subagent_invocations()
        if cv is not None:
            return cv
    except Exception:  # noqa: S110
        pass
    return getattr(ctx, "_current_subagent_invocations", []) or []


def _add_insight_schema() -> ToolSchema:
    return ToolSchema(
        name="canvas_add_insight",
        description=(
            "把**一条** insight (洞察 + 解读) 加到画布. 用于报告里的核心分析段. "
            "**典型用法**: subagent 拉完数据 + 画完 chart 后, 一次产一条 insight 调本工具, "
            "narrative 引数据 + 解读. 多 insight 不要 inline 塞 subagent_return.payload, "
            "用本工具一段一段调, 每条 narrative 100-400 字 (硬上限 1200; LLM 单 call 输出小, 不偷懒)."
        ),
        input_schema={
            "type": "object",
            "required": ["title", "thesis", "narrative", "evidence_section_ids"],
            "properties": {
                "title": {
                    "type": "string",
                    "description": "insight 标题 (≤ 30 字), 例: '体系维度合规率分化'",
                },
                "thesis": {
                    "type": "string",
                    "description": ("1 句论点, ≤ 50 字, 例: '信息技术体系合规率 50% 远超均值'"),
                },
                "narrative": {
                    "type": "string",
                    "description": (
                        '解读段, 100-400 字 (硬上限 1200, 按论点自然长度). **必须**含具体数字 / dim_value 引用. 禁纯描述无数据 ("业绩有压力"类).'
                    ),
                },
                "evidence_section_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "支撑证据的视觉 section ID 列表 (chart 或 image). **优先用 ν1.5 "
                        "simple int** (从 <available_refs> chart 或 image 列表选 id 数字, "
                        'e.g. "1" / "2"); 也接受 add_chart / add_image 返过的 '
                        "real `sec_xxx` (passthrough). 必须先 add_chart/add_image 再 "
                        "add_insight; 编 ID 拒绝. 至少 1 个."
                    ),
                },
                "impact": {
                    "type": "object",
                    "description": ("(可选) 量化业务影响. 字段: quantity / unit / direction / timeframe / note."),
                    "properties": {
                        "quantity": {"type": "number"},
                        "unit": {"type": "string"},
                        "direction": {"type": "string", "enum": ["up", "down", "flat"]},
                        "timeframe": {"type": "string"},
                        "note": {"type": "string"},
                    },
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "med", "low"],
                    "description": "(可选) 置信度",
                },
                "anchor_heading": {
                    "type": "string",
                    # Batch B2-4: like add_chart, ordering was never wired (consumer checks charts only); make the promise truthful.
                    "description": "（可选）只作标记，**不会**移动这张卡片的位置。",
                },
            },
        },
    )


async def _add_insight(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}

    title = str(args.get("title") or "").strip()
    thesis = str(args.get("thesis") or "").strip()
    narrative = str(args.get("narrative") or "").strip()
    evidence_ids = args.get("evidence_section_ids") or []

    # ─── Sprint ν1.5 — translate simple-int IDs FIRST (closes CTO ν1.5 P0-A1) ─
    # When peer passes evidence_section_ids=["1"], missing-check must use translated sec_xxx to find actual existence.
    # Also translate narrative [num:...:src:N]/[ref:N] markers.
    try:
        _ν15_al = run_ref_allowlist(ctx)
        if _ν15_al is not None:
            # Markers inside narrative.
            narrative = _ν15_al.translate_marker(narrative)
            # Each evidence_section_ids item: simple int → real ID.
            if isinstance(evidence_ids, list):
                evidence_ids = [_ν15_al.resolve(str(eid)) if eid is not None else eid for eid in evidence_ids]
    except Exception as _ν15_thin_exc:
        logger.warning(
            "ν1.5.thin_translate_failed err=%s",
            _ν15_thin_exc,
        )

    # ─── Field validation ─────────────────
    if not title or len(title) > 60:
        return {"error": "title required, ≤ 60 字"}
    if not thesis:
        return {"error": "thesis required (1 句论点 ≤ 50 字)"}
    if len(thesis) > 100:
        return {"error": f"thesis too long ({len(thesis)} 字), ≤ 50 字"}
    if not narrative or len(narrative) < 30:
        return {"error": f"narrative 太短 ({len(narrative)} 字), 至少 30 字"}
    if len(narrative) > 1200:
        # Default-ghostwriter decision (2026-08-13): cards are the default report shape; large models write prose within them.
        # Raise the character cap rather than forcing interpretations into telegraphic text (synchronized with AnalysisPayload _NARR_MAX).
        return {"error": f"narrative 太长 ({len(narrative)} 字), ≤ 1200 字"}
    if not isinstance(evidence_ids, list) or len(evidence_ids) == 0:
        return {"error": "evidence_section_ids 至少 1 个 (引 add_chart 的 sec_xxx)"}

    if ctx.canvas_store is None or not ctx.session_id:
        return {"error": "canvas_store/session_id not configured on ctx"}

    # ─── P0 #4: verify every evidence_section_id exists in canvas_store ───
    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    existing = await ctx.canvas_store.list_sections(canvas.id)
    existing_ids = {s.id for s in existing}
    missing = [sid for sid in evidence_ids if sid not in existing_ids]
    if missing:
        return {
            "error": (
                f"evidence_section_ids 含不存在的 ID: {missing}. "
                "**先 add_chart 再 canvas_add_insight 引用返过的 sec_xxx**, "
                f"现 canvas 里有 {len(existing_ids)} 个 section."
            ),
        }

    # ─── Sprint ν1: fix thin-path bypass (CTO r1 P0-3 closure) ───
    # M157 thin path: peer writes [ref:chunk_xxx] / [num:...:src:tl_xxx] directly in narrative,
    # bypassing subagent_return validation. Validate narrative here using the same hot-read flag.
    import os as _os

    if _os.environ.get("BOWHEAD_REF_VALIDATE_STRICT", "1") == "1":
        try:
            artifact_store_obj = getattr(ctx, "artifact_store", None)
            tool_invocations = _current_invocations(ctx)
            if artifact_store_obj is not None:
                from bowhead_core._ref_audit import (
                    emit_ref_audit_event,
                )
                from bowhead_core._ref_validation import (
                    _build_real_refs,
                    _normalize_whitespace,
                    _scan_markers,
                )

                real_refs = await _build_real_refs(
                    canvas_id=canvas.id,
                    session_id=ctx.session_id,
                    user_id=ctx.user_id or "",
                    canvas_store=ctx.canvas_store,
                    artifact_store=artifact_store_obj,
                    tool_invocations=tool_invocations,
                )
                _thin_errs = _scan_markers(narrative, "args.narrative", real_refs)
                if _thin_errs:
                    _ν_thin_retry = getattr(ctx, "_ν_thin_retry_count", 0)
                    _turn = getattr(ctx, "turn_id", "") or ""
                    if _ν_thin_retry >= 2:
                        for e in _thin_errs:
                            narrative = narrative.replace(e["value"], "")
                        narrative = _normalize_whitespace(narrative)
                        await emit_ref_audit_event(
                            _turn,
                            "thin_strip_degrade",
                            {"errors": _thin_errs[:20], "retry_count": _ν_thin_retry},
                            user_id=ctx.user_id or "",
                            workspace_id=ctx.workspace_id or "",
                        )
                    else:
                        ctx._ν_thin_retry_count = _ν_thin_retry + 1
                        await emit_ref_audit_event(
                            _turn,
                            "thin_validate_reject",
                            {"errors": _thin_errs[:20], "retry_count": _ν_thin_retry},
                            user_id=ctx.user_id or "",
                            workspace_id=ctx.workspace_id or "",
                        )
                        err_lines = "\n".join(f"  - {e['kind']} ref={e['ref_id']}" for e in _thin_errs[:5])
                        return {
                            "error": ("ν1 narrative ref validation failed:\n" + err_lines + "\n仅引用 tool 返回的真 ID 或不引用."),
                            "ν_ref_retry": True,  # Does not count as tool_failure
                            "ref_errors": _thin_errs[:20],
                        }
        except Exception as _ν_thin_exc:
            logger.warning(
                "ν1.thin_validate_failed err=%s — falling open",
                _ν_thin_exc,
            )

    # citation-audit M1-0 (thin-path generation upgrade): this was the last direct-to-storage gen2 path.
    # The former reason against in-place [N] conversion (no sources lockstep → empty popovers;
    # see historical git comments) was resolved by upsert_turn_sources_section: registry attaches to
    # parent_bus (turn-shared numbering with writer-loop/composer), synchronizing [N] text conversion
    # with sources upsert. M3-2 removed the kill-switch legacy-mode branch.
    try:
        from bowhead_core import citation_marks as _cm

        # ── Hygiene (independent of bus, impl-review B-P3-13) ───
        # ① Strip model-authored bare [N] (SEC-P0: hardcoded peer [1] rendered as clickable trusted citation
        #   with verified status; valid [N] can be minted only by the registry below).
        # ② Strip untranslated numeric [ref:K] residues (allowlist absent/missed),
        #   or convert synthesizes garbage source entries for 3 (impl-review C-P2-4).
        #   Actual chunk IDs ([ref:chunk_xxx]) are unaffected and resolved normally by convert.
        _numeric_ref_re = __import__("re").compile(r"\s?\[ref:\d{1,3}\]")
        narrative = _cm.strip_untrusted_numbered(narrative)
        narrative = _numeric_ref_re.sub("", narrative)
        thesis = _cm.strip_untrusted_numbered(thesis)
        thesis = _numeric_ref_re.sub("", thesis)

        _bus_reg = getattr(ctx, "parent_bus", None)
        if _bus_reg is not None:
            from bowhead_core.canvas_composers.answer_decomposer import (
                _build_value_to_source_map as _bv2s,
            )
            from bowhead_core.sources_resolver import (
                enrich_source_labels,
                get_turn_sources_registry,
                upsert_turn_sources_section,
            )

            _invs = _current_invocations(ctx)
            # Clarification-resume generation fix (B-P2-7): seed the new bus's empty registry from
            # persisted sources so numbering continues across generations. Creation/seeding share one entry point.
            _reg = await get_turn_sources_registry(
                _bus_reg,
                canvas_store=ctx.canvas_store,
                canvas_id=canvas.id,
                turn_id=ctx.turn_id,
            )
            _reg.ingest_tool_invocations(_invs)

            def _to_numbered(_txt: str) -> str:
                # :src: explicit attribution maps content-addressed IDs directly to numbers (#11 fidelity).
                _t = _cm.convert_gen2_src(
                    _txt,
                    sources=_reg.sources,
                    id_to_index=_reg.id_to_index,
                    tool_invocations=_invs,
                )
                # Bake plain residues, preserving units.
                _t = _cm.strip_legacy(_t)
                # Bare value → [N]; adjacency skip prevents double marking.
                _v2s = _bv2s(_invs)
                if _v2s:
                    _t = _cm.bind_to_numbered(
                        _t,
                        value_to_source=_v2s,
                        sources=_reg.sources,
                        id_to_index=_reg.id_to_index,
                        tool_invocations=_invs,
                    )
                # Bare artifact ID → [N] in the same numbering space.
                return _reg.resolve_bare_ids(_t, _invs)

            narrative = _to_numbered(narrative)
            thesis = _to_numbered(thesis)
            if not _reg.is_empty():
                _entries = _reg.sources
                with contextlib.suppress(Exception):
                    _entries = await enrich_source_labels(
                        _reg.sources,
                        getattr(ctx, "artifact_store", None),
                        user_id=ctx.user_id or "",
                    )
                from bowhead_core.report_chrome_text import chrome_lang as _chrome_lang

                await upsert_turn_sources_section(
                    canvas_store=ctx.canvas_store,
                    bus=_bus_reg,
                    canvas_id=canvas.id,
                    turn_id=ctx.turn_id,
                    entries=_entries,
                    lang=_chrome_lang(getattr(ctx, "language_context", None)),
                )
    except Exception as _g3_exc:
        logger.warning(
            "canvas_add_insight.gen3_bind_failed err=%s — falling open",
            _g3_exc,
        )

    # ─── Persist to Canvas ─────────────────
    payload: dict[str, Any] = {
        "title": title,
        "thesis": thesis,
        "narrative": narrative,
        "evidence": [{"kind": "chart", "chart_section_id": sid} for sid in evidence_ids],
    }
    impact = args.get("impact")
    if isinstance(impact, dict):
        payload["impact"] = impact
    confidence = args.get("confidence")
    if confidence in ("high", "med", "low"):
        payload["confidence"] = confidence
    anchor = args.get("anchor_heading")
    if isinstance(anchor, str) and anchor.strip():
        payload["anchor_heading"] = anchor.strip()

    stored = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="insight_card",
        payload=payload,
        by_turn_id=ctx.turn_id,
    )

    bus = getattr(ctx, "parent_bus", None)
    if bus is not None:
        try:
            await bus.emit(
                OrchestratorEvent(
                    event_type="canvas.section_added",  # type: ignore[arg-type]
                    turn_id=ctx.turn_id,
                    payload={
                        "canvas_id": canvas.id,
                        "section": stored.to_report_section(),
                        "seq": stored.seq,
                    },
                )
            )
        except Exception as e:
            logger.warning("canvas_add_insight: SSE emit failed: %s", e)

    return {
        "section_id": stored.id,
        "canvas_id": canvas.id,
        "kind": "insight_card",
        "title": title,
        "evidence_count": len(evidence_ids),
    }


# ─────────────────────────────────────────────────────────────
# canvas_add_html_artifact (#83 Case B): embed AI-generated HTML artifacts in Canvas
# ─────────────────────────────────────────────────────────────
def _add_html_artifact_schema() -> ToolSchema:
    return ToolSchema(
        name="canvas_add_html_artifact",
        description=(
            "把 AI 生成、已物化的单文件静态 HTML artifact 嵌入画布作安全阅读视图. "
            "HTML 的正文、数字、表格和图形必须已经写入文件，不能依赖 JavaScript、React mount、"
            "运行时 fetch、表单或外部资源. 前端会净化主动内容、关闭外网，并用不授予任何 token 的 "
            "sandbox iframe 渲染；被净化后没有静态正文或内嵌 raster 图片的页面会降级为不可预览. "
            "artifact 必须以 text/html 或 application/xhtml+xml MIME 持久化；扩展名不能替代 MIME. "
            "`data` 只记录生成输入快照与可信来源，不会在浏览器运行时注入页面. "
            "**禁止** 用作 external URL 嵌入（那走 canvas_add_external_iframe）."
        ),
        input_schema={
            "type": "object",
            "required": ["artifact_id", "title"],
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "web-artifacts-builder 生成的 HTML artifact_id (art_xxx)",
                },
                "title": {
                    "type": "string",
                    "description": "section 标题（≤ 40 字）",
                },
                "height": {
                    "type": "integer",
                    "description": "静态阅读框高度像素（默认 480，范围 200–1200）",
                },
                "data": {
                    "type": "object",
                    "description": (
                        "生成这份静态 HTML 时使用的 verified 数据快照（JSON object）. 只用于来源说明，不会注入 iframe；HTML 必须已包含可读结果."
                    ),
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "生成数据对应的 citation 编号（footer 显示 '[2] [6]'）",
                },
            },
        },
    )


_HTML_ARTIFACT_DATA_MAX = 200 * 1024  # Generated-input snapshot JSON cap


def _clean_artifact_data(raw: Any) -> dict[str, Any] | None:
    """校验生成输入快照: 必须是 JSON object 且序列化后 ≤ 200KB."""
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        encoded = json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > _HTML_ARTIFACT_DATA_MAX:
        return None
    return raw


async def _add_html_artifact(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}
    aid = str(args.get("artifact_id") or "").strip()
    if not aid:
        return {"error": "artifact_id required"}
    if ctx.artifact_store is None:
        return {"error": "artifact_store not configured"}
    if ctx.canvas_store is None or not ctx.session_id:
        return {"error": "canvas_store/session_id not configured"}

    # #54 permanent fix (2026-04-29): owner check prevents unauthorized artifact IDs recalled from another user/session.
    art = await ctx.artifact_store.get(aid, check_created_by=ctx.user_id)
    if art is None:
        return {"error": f"artifact {aid!r} not found"}
    persisted_mime = str(art.mime or "").split(";", 1)[0].strip().lower()
    mime_ok = persisted_mime in ("text/html", "application/xhtml+xml")
    if not mime_ok:
        return {"error": f"artifact mime {art.mime!r} is not html"}
    from bowhead_core.html_preview_policy import html_preview_limit_bytes

    max_bytes = html_preview_limit_bytes()
    if art.size > max_bytes:
        return {
            "error": f"HTML artifact is too large ({art.size} bytes); the current Team limit is {max_bytes} bytes. Reduce the file or ask a Team administrator to change Agents > Run limits > HTML reports.",
            "code": "html_artifact_too_large",
            "maximum_bytes": max_bytes,
        }

    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    title = str(args.get("title") or "Generated Page").strip()[:40]
    try:
        height = int(args.get("height") or 480)
    except (TypeError, ValueError):
        height = 480
    height = max(200, min(1200, height))
    # `data` is generation-input provenance, not a runtime iframe injection.
    inj_data = _clean_artifact_data(args.get("data"))
    # LLMs often violate the contract with a scalar (sources:6) instead of a list; prevent TypeError; bool is not a source index.
    raw_sources = args.get("sources")
    sources = [int(s) for s in raw_sources if isinstance(s, (int, float)) and not isinstance(s, bool)] if isinstance(raw_sources, list) else []
    payload: dict[str, Any] = {
        "artifact_id": aid,
        "title": title,
        "height": height,
        "sandbox": True,
        "unverified": inj_data is None,
    }
    if inj_data is not None:
        payload["data"] = inj_data
    if sources:
        payload["sources"] = sources
    stored = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="html_artifact",
        payload=payload,
        by_turn_id=ctx.turn_id,
    )
    bus = getattr(ctx, "parent_bus", None)
    if bus is not None:
        try:
            await bus.emit(
                OrchestratorEvent(
                    event_type="canvas.section_added",  # type: ignore[arg-type]
                    turn_id=ctx.turn_id,
                    payload={"canvas_id": canvas.id, "section": stored.to_report_section(), "seq": stored.seq},
                )
            )
        except Exception as e:
            logger.warning("canvas_add_html_artifact: SSE emit failed: %s", e)
    return {
        "section_id": stored.id,
        "canvas_id": canvas.id,
        "kind": "html_artifact",
        "artifact_id": aid,
        "title": title,
    }


# ─────────────────────────────────────────────────────────────
# canvas_add_external_iframe (#125 Case A): embed external URLs in Canvas
# ─────────────────────────────────────────────────────────────
def _add_external_iframe_schema() -> ToolSchema:
    return ToolSchema(
        name="canvas_add_external_iframe",
        description=(
            "把一个**外部 https URL**（非 datarc）作为 iframe 嵌到画布. "
            "常见场景: 用户明确说'把 xxx.com/xxx 嵌入我的报告' / 嵌 own-domain dashboard. "
            "**只接受 https**. 大部分公网站点带 X-Frame-Options: DENY 会白屏，前端带 "
            "'新标签打开' 逃生口. **禁止** 用作 datarc 卡片（datarc 数据一律走原生 "
            "add_chart，不再嵌 iframe）或 AI 生成 HTML（走 canvas_add_html_artifact）."
        ),
        input_schema={
            "type": "object",
            "required": ["url", "title"],
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要嵌的 https URL（**必须 https**；http 拒绝）",
                },
                "title": {
                    "type": "string",
                    "description": "section 标题（≤ 40 字）",
                },
                "height": {
                    "type": "integer",
                    "description": "iframe 高度像素，默认 420",
                },
            },
        },
    )


async def _add_external_iframe(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}
    url = str(args.get("url") or "").strip()
    if not url:
        return {"error": "url required"}
    from urllib.parse import urlsplit

    from bowhead_core.ssrf_guard import SsrfBlocked, validate_url

    # Canvas embeds only web pages: local outbound policy allows any address, but iframe src with non-web schemes
    # such as javascript:/data: executes content inside Bowhead Canvas, independent of outbound policy.
    parts = urlsplit(url)
    if (parts.scheme or "").lower() not in {"http", "https"} or not parts.hostname:
        return {"error": "url must be an http(s) web page"}
    try:
        validate_url(url)
    except (SsrfBlocked, ValueError):
        return {"error": "url is not allowed for this deployment"}
    if ctx.canvas_store is None or not ctx.session_id:
        return {"error": "canvas_store/session_id not configured"}

    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    title = str(args.get("title") or "External Page").strip()[:40]
    try:
        height = int(args.get("height") or 420)
    except (TypeError, ValueError):
        height = 420
    height = max(200, min(1200, height))
    payload: dict[str, Any] = {"url": url, "title": title, "height": height}
    stored = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="external_iframe",
        payload=payload,
        by_turn_id=ctx.turn_id,
    )
    bus = getattr(ctx, "parent_bus", None)
    if bus is not None:
        try:
            await bus.emit(
                OrchestratorEvent(
                    event_type="canvas.section_added",  # type: ignore[arg-type]
                    turn_id=ctx.turn_id,
                    payload={"canvas_id": canvas.id, "section": stored.to_report_section(), "seq": stored.seq},
                )
            )
        except Exception as e:
            logger.warning("canvas_add_external_iframe: SSE emit failed: %s", e)
    return {
        "section_id": stored.id,
        "canvas_id": canvas.id,
        "kind": "external_iframe",
        "url": url,
        "title": title,
    }


# ─────────────────────────────────────────────────────────────
# S3: canvas_add_spec_block: structured diagrams (attribution chains/metric trees/flows)
# ─────────────────────────────────────────────────────────────
_SPEC_BLOCK_FORMATS = ("graph",)


def _clean_spec_graph(
    raw_nodes: Any,
    raw_edges: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """清洗 LLM 给的 nodes/edges: 节点须 id+label; 边两端须已存在节点. 无有效节点返 None."""
    if not isinstance(raw_nodes, list):
        return None
    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for n in raw_nodes:
        if not isinstance(n, dict) or not n.get("id") or not n.get("label"):
            continue
        nid = str(n["id"])
        clean: dict[str, Any] = {"id": nid, "label": str(n["label"])}
        if n.get("emphasis") in ("highlight", "muted"):
            clean["emphasis"] = n["emphasis"]
        nodes.append(clean)
        node_ids.add(nid)
    if not nodes:
        return None
    edges: list[dict[str, Any]] = []
    for e in raw_edges if isinstance(raw_edges, list) else []:
        if not isinstance(e, dict):
            continue
        f, t = str(e.get("from") or ""), str(e.get("to") or "")
        if f not in node_ids or t not in node_ids:
            continue
        clean_e: dict[str, Any] = {"from": f, "to": t}
        if e.get("label"):
            clean_e["label"] = str(e["label"])
        edges.append(clean_e)
    return nodes, edges


def _add_spec_block_schema() -> ToolSchema:
    return ToolSchema(
        name="canvas_add_spec_block",
        description=(
            "把一张**结构化图**(节点+有向边)嵌到画布. 适用 **关系/流向类**而非表格数据 —— "
            "归因链(谁导致谁)、指标树/杜邦分解、流程图、状态机. **不要**用于: 普通数值图表"
            "(走 add_chart)、AI 生成 HTML(canvas_add_html_artifact). "
            "format='graph'; nodes=[{id,label,emphasis?}], edges=[{from,to,label?}]; "
            "emphasis: highlight(结论/重点)|muted(次要)|省略=普通; direction TB(上下)|LR(左右)."
        ),
        input_schema={
            "type": "object",
            "required": ["format", "nodes", "edges"],
            "properties": {
                "format": {
                    "type": "string",
                    "enum": list(_SPEC_BLOCK_FORMATS),
                    "description": "图格式, 目前仅 'graph'",
                },
                "title": {"type": "string", "description": "标题(可选, ≤ 40 字)"},
                "direction": {
                    "type": "string",
                    "enum": ["TB", "LR"],
                    "description": "布局方向, 默认 TB(上下)",
                },
                "nodes": {
                    "type": "array",
                    "description": "节点",
                    "items": {
                        "type": "object",
                        "required": ["id", "label"],
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "emphasis": {"type": "string", "enum": ["default", "highlight", "muted"]},
                        },
                    },
                },
                "edges": {
                    "type": "array",
                    "description": "有向边 (from→to)",
                    "items": {
                        "type": "object",
                        "required": ["from", "to"],
                        "properties": {
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "label": {"type": "string"},
                        },
                    },
                },
            },
        },
    )


async def _add_spec_block(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    if args.get("_args_parse_error"):
        return {"error": f"args parse failed: {args['_args_parse_error']}"}
    fmt = str(args.get("format") or "").strip().lower()
    if fmt not in _SPEC_BLOCK_FORMATS:
        return {"error": f"unsupported spec_block format: {fmt!r} (支持: {_SPEC_BLOCK_FORMATS})"}
    cleaned = _clean_spec_graph(args.get("nodes"), args.get("edges"))
    if cleaned is None:
        return {"error": "nodes required (非空数组, 每个须 id+label)"}
    nodes, edges = cleaned
    if ctx.canvas_store is None or not ctx.session_id:
        return {"error": "canvas_store/session_id not configured"}

    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    payload: dict[str, Any] = {"format": fmt, "nodes": nodes, "edges": edges}
    title = str(args.get("title") or "").strip()[:40]
    if title:
        payload["title"] = title
    if args.get("direction") in ("TB", "LR"):
        payload["direction"] = args["direction"]
    stored = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="spec_block",
        payload=payload,
        by_turn_id=ctx.turn_id,
    )
    bus = getattr(ctx, "parent_bus", None)
    if bus is not None:
        try:
            await bus.emit(
                OrchestratorEvent(
                    event_type="canvas.section_added",  # type: ignore[arg-type]
                    turn_id=ctx.turn_id,
                    payload={"canvas_id": canvas.id, "section": stored.to_report_section(), "seq": stored.seq},
                )
            )
        except Exception as e:
            logger.warning("canvas_add_spec_block: SSE emit failed: %s", e)
    return {
        "section_id": stored.id,
        "canvas_id": canvas.id,
        "kind": "spec_block",
        "format": fmt,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "title": title or None,
    }


# ─────────────────────────────────────────────────────────────
# CSV reading and column-type inference
# ─────────────────────────────────────────────────────────────
# ID-column denylist: integer columns with these name suffixes are codes/identifiers, not measures.
_ID_COL_SUFFIXES = ("编码", "ID", "id", "序号", "工号", "code", "_id", "_code")
# Preferred categorical names: these terms indicate grouping/descriptive columns, preferred for axes.
_CAT_HINT_NAMES = ("名称", "分类", "类型", "category", "type", "name", "group")


def _is_true_numeric(col: ChartColumn) -> bool:
    """是否真数值列（排除 '部门编码' / '申请人工号' 这类 int 编码）."""
    if col.type != "numeric":
        return False
    key = str(col.key)
    return not any(key.endswith(s) for s in _ID_COL_SUFFIXES)


def _sibling_numeric(primary: ChartColumn, true_nums: list[ChartColumn]) -> ChartColumn:
    """给 `primary` 挑第二条序列：**同族列优先**。

    宽表里同一指标的不同口径共用词根（`营业额` / `营业额_6月` / `营业额环比变化`），
    而无关指标（`客流量`、`有效门店数`）只是恰好排在前面。按列表顺序取 `true_nums[1]`
    会把「营业额 vs 6月营业额」配成「营业额 vs 客流量」——量纲都不同，图更误导。
    """
    others = [c for c in true_nums if c is not primary]
    if not others:
        return primary
    base = str(primary.key)
    kin = [c for c in others if str(c.key).startswith(base) or base.startswith(str(c.key))]
    return kin[0] if kin else others[0]


def _lead_dim_col(
    rows: list[dict[str, Any]],
    dims: list[ChartColumn],
    *,
    prefer: ChartColumn | None = None,
) -> ChartColumn | None:
    """Choose the dimension that best distinguishes the rows.

    Prefer the requested column when all its values are unique. Otherwise choose the
    column with the most distinct values, keeping column order for ties. If every
    column is constant, retain the preference or first column and let validation
    report the ambiguity. A single axis cannot represent every compound key; table
    and pivot handling preserve the additional dimensions separately.
    """
    if not dims:
        return None
    if not rows:
        return prefer or dims[0]

    n = len(rows)

    def distinct(col: ChartColumn) -> int:
        return len({str(r.get(col.key)) for r in rows})

    if prefer is not None and distinct(prefer) == n:
        return prefer
    best = max(dims, key=lambda c: (distinct(c), -dims.index(c)))
    return best if distinct(best) > 1 else (prefer or dims[0])


def _pivotable_group_dim(
    rows: list[dict[str, Any]],
    dims: list[ChartColumn],
    lead: ChartColumn | None,
    picked: list[ChartColumn],
) -> ChartColumn | None:
    """Retain a second dimension only when the input is safely pivotable.

    Require exactly one named numeric series, at least two values on each axis, and
    sufficient cross-product coverage under the shared LongTableFillRatio threshold.
    Apply the same input-shape contract to explicit and automatic charts without
    guessing the intended chart kind.
    """
    if lead is None or len(picked) != 1:
        return None
    candidates = [c for c in dims if c.key != lead.key]
    if not candidates:
        return None

    from bowhead_core.chart_style_registry_generated import CHART_STYLE_PRESENTATION_POLICY

    group = _lead_dim_col(rows, candidates)
    if group is None:
        return None

    def values(col: ChartColumn) -> set[str]:
        return {str(r.get(col.key)) for r in rows}

    x_vals, group_vals = values(lead), values(group)
    if len(x_vals) < 2 or len(group_vals) < 2:
        return None
    fill = len(rows) / (len(x_vals) * len(group_vals))
    if fill < CHART_STYLE_PRESENTATION_POLICY["normalization"]["minimumLongTableFillRatio"]:
        return None
    logger.info(
        "add_chart.kept_group_dim x=%s group=%s fill=%.2f rows=%d",
        lead.key,
        group.key,
        fill,
        len(rows),
    )
    return group


def _explicit_series(
    args: dict[str, Any],
    rows: list[dict[str, Any]],
    cols: list[ChartColumn],
    *,
    preserve_dimensions: bool = False,
) -> tuple[list[dict[str, Any]], list[ChartColumn]] | None:
    """调用方显式点名要画的数值列 → 按点名裁列。没点名 / 一个都对不上 → None（走启发式）。

    维度轴（cat/date/geo）自动保留在最前，模型只需要点数值列。
    未知 key 忽略并记一行日志——宁可少画一条序列，也不要因为一个错 key 整张图不出。
    """
    raw = args.get("series")
    if not isinstance(raw, list) or not raw:
        return None
    wanted = [str(k) for k in raw if isinstance(k, (str, int, float))]
    by_key = {c.key: c for c in cols}
    unknown = [k for k in wanted if k not in by_key]
    if unknown:
        logger.info("add_chart.series_unknown_keys keys=%s known=%s", unknown[:5], [c.key for c in cols][:12])
    picked = [by_key[k] for k in wanted if k in by_key]
    if not picked:
        return None
    dims = [c for c in cols if c.type in ("date", "categorical", "geo") and c not in picked]
    # Do not take dims[:1] indiscriminately. A constant filter column may precede the
    # actual grouping column. Tables can also require multiple dimensions to identify
    # a compound key, while a chart axis selects one and may pivot the other.
    if preserve_dimensions:
        kept_dims = dims
    elif str(args.get("force_kind") or "") == "table":
        kept_dims = [c for c in dims if len({str(r.get(c.key)) for r in rows}) > 1] or dims[:1]
    else:
        lead = _lead_dim_col(rows, dims)
        kept_dims = [lead] if lead is not None else []
        group = _pivotable_group_dim(rows, dims, lead, picked)
        if group is not None:
            kept_dims.append(group)
    kept_cols = kept_dims + picked
    kept_keys = [c.key for c in kept_cols]
    return [{k: r.get(k) for k in kept_keys} for r in rows], kept_cols


def _needs_col_pruning(cols: list[ChartColumn]) -> bool:
    """这份列集合是否宽到必须裁列才画得出.

    判据与 `infer_chart_spec` 的退表闸**同源** —— 按「画不出的两个原因」各写一条，
    而不是按总列数拍一个数：

    - **序列太多**：多到读不动（上限见下）。
    - **维度太多**：判定树规则 9 在 `cat >= 3` 时退表，而裁列恰好能把维度压到 1，
      所以这条归裁列管。

    ⚠ 原来这里写的是 `len(cols) > 6`（总列数）。它与序列上限**会打架**：1 个维度 +
    6 个数值 = 7 列，恰好在序列上限内、却被总列数闸判成要裁——把一张画得出的
    六序列图裁成两列。两条子判据必须各自表达自己那件事，别用一个总数糊过去。

    ⚠ 序列这条按**同族上限**放行（`_MAX_SERIES_SAME_FAMILY`），比退表闸松一档：
    这里拿不到 rows，算不出量纲族，而**裁早了就没法在下游按族分档**了
    （`各品类月度趋势` 10 列同族，裁到 6 之后退表闸再宽也救不回被丢掉的 4 个品类）。
    松着放过去，异族那档由退表闸按真实族数收口——**宁可让下游多判一次，
    也不能在信息不全的上游先砍**。
    """
    from bowhead_core.chart.infer import _MAX_SERIES_SAME_FAMILY

    n_num = sum(1 for c in cols if c.type == "numeric")
    n_dim = len(cols) - n_num
    return n_num > _MAX_SERIES_SAME_FAMILY or n_dim > 2


def _column_total(rows: list[dict[str, Any]], col: ChartColumn) -> float:
    """一列的合计（绝对值求和）。

    `_cap_time_series` 给长表排名用的也是这条（全部数值列的 |值| 求和），两条截断路径
    必须同一把尺子——否则同一份数据走长表和走宽表会留下**不同的**前 15 / 前 12。
    用 |值| 是因为一条全是负数的「利润」序列同样是这张图上最重要的线之一。
    """
    acc = 0.0
    for r in rows:
        v = r.get(col.key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            acc += abs(float(v))
    return acc


def _prune_cols_for_hint(
    rows: list[dict[str, Any]],
    cols: list[ChartColumn],
    *,
    hint: str,
    sort_by: str | None,
) -> tuple[list[dict[str, Any]], list[ChartColumn], SeriesCut | None]:
    """hint 明确 + 列数太多时，裁列到 [1 个 cat/date, 1 个 num]（或 [cat, sort_by]）.

    #76 扩展：
    - ID 列黑名单（编码/ID/序号/工号/code）排除出 numeric 候选
    - cat 优选列名含"名称/分类/类型"的
    - correlation hint → 保留 2 个 numeric（scatter 需要 x/y 都是数值）

    第三个返回值 `(画了几条, 一共几条)`：**只有宽表 pivot 那一支**会给出来——
    那一支丢掉的是「同一个指标的其余类目」，用户会把它读成「这就是全部类目」。
    其余分支丢的是**别的指标**（18 列宽表里挑出 `营业额`），说成「只画了 1/18 条系列」
    是另一句假话，所以它们一律返回 `None`。判据必须长在**做那一刀的分支上**，
    不能在调用方按「数值列少了几个」倒推（[[feedback_one_concept_many_implementations]]）。
    """
    if len(cols) <= 2:
        return rows, cols, None

    # correlation / compare both require **two** numeric series.
    #   - correlation → scatter x/y axes
    #   - compare → A vs B by definition; one column gives a ranking, not a comparison.
    #
    # compare previously entered the generic branch below and was pruned to one column; observed in 116 prod 1.0.79:
    # an 18-column table containing revenue / June revenue / month-over-month revenue change produced a chart titled
    # provincial revenue/profit comparison (July vs June) with only province/revenue, identical to the adjacent revenue ranking.
    # Title promised comparison; data had no comparison column.
    # composition likewise (chart-config-ux batch 1): stacked structure by definition combines multiple components.
    # Pruning to one column invalidates _kind_from_intent's composition guard (≥2 numeric columns),
    # so upstream pruning defeats its own intent. Pruning policy must follow intent or the two undermine each other.
    if hint in ("correlation", "compare", "composition"):
        true_nums = [c for c in cols if _is_true_numeric(c)]
        if len(true_nums) >= 2:
            # Prefer sort_by as the first series.
            primary = next((c for c in true_nums if c.key == sort_by), None) or true_nums[0]
            secondary = _sibling_numeric(primary, true_nums)
            kept_cols = [primary, secondary]
            kept_keys = [primary.key, secondary.key]
            # compare/composition retain a dimension axis; otherwise infer interprets two pure numeric columns as a KPI card.
            # correlation is scatter with numeric x/y, so keep its existing dimensionless behavior.
            if hint in ("compare", "composition"):
                dims = [c for c in cols if c.type in ("date", "categorical", "geo")]
                # Like _explicit_series, name-based preference counts only if it **actually distinguishes rows**;
                # otherwise use the first distinguishing column (both pruning branches require the same criterion).
                named = next((c for c in dims if any(h in str(c.key) for h in _CAT_HINT_NAMES)), None)
                preferred_dim = _lead_dim_col(rows, dims, prefer=named)
                if preferred_dim is not None:
                    kept_cols = [preferred_dim, *kept_cols]
                    kept_keys = [preferred_dim.key, *kept_keys]
            new_rows = [{k: r.get(k) for k in kept_keys} for r in rows]
            return new_rows, kept_cols, None

    # Find cat/date/geo axis: prefer a name containing categorical hint terms.
    cat_candidates = [c for c in cols if c.type in ("date", "categorical", "geo")]
    if not cat_candidates:
        return rows, cols, None
    # ⚠ Fallback is not cat_candidates[0]: like _explicit_series (see _lead_dim_col),
    # name preference counts only if it truly identifies rows; otherwise choose the dimension with most distinct values,
    # not the first column (often a constant filtering dimension). compare/composition above already changed this,
    # but this generic branch was missed: the recurring problem of two implementations for one concept.
    named = next((c for c in cat_candidates if any(h in str(c.key) for h in _CAT_HINT_NAMES)), None)
    preferred_cat = _lead_dim_col(rows, cat_candidates, prefer=named) or cat_candidates[0]

    # Find numeric axis.
    true_nums = [c for c in cols if _is_true_numeric(c)]
    if not true_nums:
        return rows, cols, None

    from bowhead_core.chart.infer import _MAX_SERIES_SAME_FAMILY, _axis_groups

    # Wide-table pivot: numeric columns exceed series capacity, but are **one metric expanded across categories**
    # (typical sandbox df.pivot_table(index='月份', columns='种类大类', …) output).
    #
    # Keeping a main metric is invalid here: there is none; each column is a category.
    # true_nums[0] below retains **the first category in column order**; 116 dogfood
    # t_da7397e4d95c requested each category's monthly revenue trend; its 9×38 wide table became
    # 9×1, retaining disposable products averaging 15,000 monthly, while the largest, women's wear, was 456 million:
    # a 30,000-fold gap. The plotted line was the smallest category, while the subtitle still claimed grouping by major category.
    #
    # ⚠ Change the criterion **only above the series limit**. Observations from 81 charts matched to source CSV:
    # all 16 cases with ≤5 numeric columns that did not retain the largest column **were correct**; highest refund-rate Top10
    # needs refund rate, not much larger revenue. Magnitude-based choice cannot generalize;
    # it applies only when all candidate columns expand categories of one metric.
    if (
        len(true_nums) > _MAX_SERIES_SAME_FAMILY
        and len(cat_candidates) == 1  # Pivot has one index column
        and len(_axis_groups(rows, true_nums)) == 1  # All share one family, hence one metric
    ):
        # ★ Rank by **total**, not _column_magnitude (the **mean** of nonzero absolute values).
        # Captions and model notes explicitly promise first N by total; rankings must match or captions lie.
        # Both measures nearly agree when categories have equal month counts, but reverse when one category
        # has high values for only two months: its mean ranks high,
        # while its yearly total is tiny ([[feedback_same_metric_new_subject_needs_revalidation]]).
        ordered = sorted(true_nums, key=lambda c: -_column_total(rows, c))[:_MAX_SERIES_SAME_FAMILY]
        ranked = {c.key for c in ordered}
        kept_nums = [c for c in true_nums if c.key in ranked]  # Preserve original column order
        kept_cols = [preferred_cat, *kept_nums]
        kept_keys = [c.key for c in kept_cols]
        logger.info(
            "add_chart.wide_pivot_kept_top_series total=%d kept=%d dropped=%s",
            len(true_nums),
            len(kept_nums),
            [c.key for c in true_nums if c.key not in ranked][:8],
        )
        return (
            [{k: r.get(k) for k in kept_keys} for r in rows],
            kept_cols,
            SeriesCut(len(kept_nums), len(true_nums), [(c.key, _column_total(rows, c)) for c in ordered]),
        )

    primary_num = (next((c for c in true_nums if c.key == sort_by), None) if sort_by else None) or true_nums[0]

    kept_keys = [preferred_cat.key, primary_num.key]
    kept_cols = [preferred_cat, primary_num]
    new_rows = [{k: r.get(k) for k in kept_keys} for r in rows]
    return new_rows, kept_cols, None


#: Marking moved to chart/units_enrich (same home as unit enrichment; both add column metadata,
#: and composer auto-charts need the same implementation). Retain private alias so existing calls/tests stay unchanged.
_mark_unknown_percent_shape = mark_unknown_percent_shape


def _apply_column_units(columns: list[ChartColumn], units: dict[str, str]) -> None:
    for column in columns:
        if column.key in units:
            column.unit = units[column.key].strip()
            column.currency_code = currency_identity(column.unit)


def _enrich_units(cols: list[ChartColumn], ctx: Any, rows: list[dict[str, Any]] | None = None, *, artifact: Any = None) -> None:
    """Look up metadata only for unchanged columns from a verified Datarc result."""
    if not can_enrich_from_current_datarc_turn(artifact, str(getattr(ctx, "turn_id", "") or "")):
        return
    try:
        client = getattr(ctx, "datarc", None)
        ws = ctx.resolve_datarc_project() if hasattr(ctx, "resolve_datarc_project") else ""
        lookup = lookup_from_source(client, ws or "")
        if lookup is not None:
            enrich_columns_with_units(cols, lookup, rows)
            # Relabel columns for this turn's output language (营业额(¥M) → GMV(¥M)), retaining qualifiers.
            # Do so **after unit enrichment**, which matches original column names exactly and would fail after renaming.
            lc = getattr(ctx, "language_context", None)
            relabel_columns_for_locale(cols, lookup, str(getattr(lc, "output_language", "") or "en-US"))
    except Exception as e:  # noqa: BLE001 —— enrichment must not break charting
        logger.debug("units.enrich_failed err=%s", e)


# Two forms where an entire cell is a formatted number. **Anchor the whole string** and require formatting markers:
#   - Thousands-separator comma: 24,303
#   - Percent sign: 15.37%
# Deliberately **exclude bare numeric strings** (1/2/3), which float() already accepts;
# strings genuinely left unchanged (month/排名) rightly belong on category axes; numeric conversion would remove the x axis.
# Also exclude currency symbols and 3.5万: neither appeared in the 1948-chart 116 corpus;
# unobserved shapes stay outside the criterion (measure false positives before adopting vocabulary).
# ⚠ Accept both signs: frontend coerceNumericCell (view-transforms.ts) has always accepted +1,234.50,
# while backend accepted only minus, yielding different parses for one column through two paths: a common bug pattern here.
_FMT_GROUPED_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_FMT_PERCENT_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*%$")


def _coerce_formatted_numeric_columns(
    rows: list[dict[str, Any]],
    fieldnames: list[str],
    declared_shapes: dict[str, str] | None = None,
) -> dict[str, str]:
    """把「整列都是格式化数字」的列就地还原成数值，返回 {列名: 单位}。

    为什么需要（116 语料 1948 张实测 16 张受影响，其中 7 张是结构性画不出）：
    `_parse_csv_with_types` 逐格 `float(v)`，而 `float("24,303")` / `float("15.37%")`
    直接抛 ⇒ 整列留成字符串 ⇒ `_infer_col_type` 的 `all(isinstance(v, (int, float)))`
    判成 categorical。后果不是「显示得难看」，是**这列再也进不了值轴**：
      · `scatter 门店面积 vs 营业额` 只剩 1 个数值列，前端直接画不出来退成表；
      · `2026年1-6月退款率月度趋势` 要画的那条率本身不是数值列；
      · 侥幸画出来的（`bar 门店盈利质量排名`）拿 `24,303` 当了类目轴标签。

    判据整列生效、不逐格生效：一列里混进一个 `24,303` 不该把整列翻成数值
    （与 `_column_values_are_dates` 用占比门槛同一个道理，只是这里更严——
    格式化数字没有「大部分是」这种中间态，混着就说明它不是一列数）。
    """
    units: dict[str, str] = {}
    for fname in fieldnames:
        vals = [r.get(fname) for r in rows]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        strs = [str(v).strip() for v in vals if isinstance(v, str) and v.strip()]
        if not strs:
            continue
        shape = (declared_shapes or {}).get(fname)
        if nums and strs and all(_FMT_PERCENT_RE.match(token) for token in strs) and shape in {"percent_fraction", "percent_points"}:
            for row in rows:
                value = row.get(fname)
                if isinstance(value, str) and value.strip():
                    row[fname] = float(Decimal(value.strip().rstrip("%").strip()) / (100 if shape == "percent_fraction" else 1))
            units[fname] = "%" if shape == "percent_points" else ""
            continue
        if nums and not all(_FMT_GROUPED_RE.match(s) or _unsafe_integer_token(s) for s in strs):
            # Mixed columns admit only thousands formatting: BI often adds commas only to large numbers (1,101,000.00 alongside 500,
            # observed in 12 columns in 116). Two forms of one quantity, unambiguous to restore.
            # Percent mixed with bare numbers differs: 13.5% beside 13.5 cannot reveal whether the bare value means 13.5% or
            # 0.135 (seven observed columns in 116). Prefer leaving the entire column unchanged for frontend whole-column range classification.
            continue
        if all(_FMT_GROUPED_RE.match(s) or _unsafe_integer_token(s) for s in strs):
            conv, unit = (lambda s: float(s.replace(",", "").lstrip("+"))), ""
        elif all(_FMT_PERCENT_RE.match(s) for s in strs):
            # 15.37% → 15.37 + unit=% (points). Do not divide by 100: frontend distinguishes fraction/points
            # through unit + value range, so division would incorrectly move it to fraction.
            conv, unit = (lambda s: float(s.rstrip("%").strip().lstrip("+"))), "%"
        else:
            continue
        for r in rows:
            v = r.get(fname)
            if v is None or not str(v).strip():
                # ⚠ Match the strs filter above: whitespace-only cells ("   ") are also empty.
                # Normalize to None (CSV parser already does this for empty strings); leaving whitespace strings makes
                # _infer_col_type classify the entire column as categorical, excluding it permanently from numeric axes.
                r[fname] = None
                continue
            token = str(v).strip().replace(",", "").rstrip("%").strip()
            if re.fullmatch(r"[+-]?\d+", token):
                integer = int(token)
                r[fname] = token if abs(integer) > 2**53 - 1 else integer
            else:
                exact = Decimal(token)
                r[fname] = (
                    str(int(exact)) if exact.is_finite() and abs(exact) > 2**53 - 1 and exact == exact.to_integral_value() else conv(str(v).strip())
                )
        units[fname] = unit
    return units


#: Besides four types, column_types may declare ratio-column **shape**.
#: Value → ChartColumn.percent_scale (also set format=percent).
_PERCENT_TYPE_OVERRIDES: dict[str, str] = {
    "percent_fraction": "fraction",  # 0.83 means 83%
    "percent_points": "points",  # 13.5 means 13.5%
}


def _unsafe_integer_token(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[+-]?\d+", value)) and abs(int(value)) > 2**53 - 1


def _parse_csv_with_types(
    raw_bytes: bytes,
    type_overrides: dict[str, str] | None = None,
    non_finite: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[ChartColumn]]:
    """从 CSV bytes 解析 + 按内容推断列类型（numeric / date / categorical / geo）.

    `non_finite`：传入时按列累计被当成空值的 inf / nan 单元格数，供调用方告诉模型。
    """
    overrides = type_overrides or {}
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(_io.StringIO(text))
    fieldnames = list(reader.fieldnames or [])
    rows: list[dict[str, Any]] = []
    for r in reader:
        out: dict[str, Any] = {}
        for k, v in r.items():
            if v is None or v == "":
                out[k] = None
                continue
            if overrides.get(k) in ("categorical", "date", "geo"):
                out[k] = v
                continue
            # Parse integers without a lossy intermediate float.
            if re.fullmatch(r"[+-]?\d+", v.strip()):
                integer = int(v)
                out[k] = integer if abs(integer) <= 2**53 - 1 else v.strip()
                continue
            try:
                f_ = float(v)
            except (ValueError, TypeError):
                out[k] = v
                continue
            # 116 1.3.51 t_6acd16abddee: month-over-month change from a zero baseline is inf in pandas and CSV.
            # int(inf) below raises uncaught OverflowError, failing the entire chart and forcing model filtering/redrawing.
            # Charts cannot plot infinity; treat as missing while retaining numeric column type.
            if not math.isfinite(f_):
                out[k] = None
                if non_finite is not None:
                    non_finite[k] = non_finite.get(k, 0) + 1
                continue
            exact = Decimal(v)
            if abs(f_) > 2**53 - 1 and exact == exact.to_integral_value():
                out[k] = str(int(exact))
            else:
                out[k] = f_
        rows.append(out)

    # float(v) above cannot parse formatted numbers (24,303 / 15.37%); restore the entire column
    # **before type inference**, or it becomes categorical and every downstream stage treats it as a dimension.
    fmt_units = _coerce_formatted_numeric_columns(
        rows, [name for name in fieldnames if overrides.get(name) not in ("categorical", "date", "geo")], overrides
    )

    cols: list[ChartColumn] = []
    for fname in fieldnames:
        t_override = overrides.get(fname)
        # Values alone cannot distinguish fractional percentages from percentage points.
        # The producer that calculated the column must declare whether it scaled by 100.
        # The declaration's precedence is defined by the percentage-shape contract.
        pct_scale = _PERCENT_TYPE_OVERRIDES.get(str(t_override or ""))
        if pct_scale:
            t = "numeric"
        elif t_override in ("numeric", "categorical", "date", "geo"):
            t = t_override  # type: ignore[assignment]
        else:
            values = [r.get(fname) for r in rows if r.get(fname) is not None]
            t = (
                "numeric"
                if values and all(isinstance(value, (int, float)) or _unsafe_integer_token(value) for value in values)
                else _infer_col_type(fname, values)
            )
        if any(isinstance(row.get(fname), str) and _FMT_PERCENT_RE.fullmatch(row[fname].strip()) for row in rows):
            t = "categorical"
            pct_scale = None
        unit = fmt_units.get(fname) or None
        col = ChartColumn(key=fname, header=fname, type=t, unit=unit)  # type: ignore[arg-type]
        if pct_scale == "fraction" and unit == "%":
            # Text 0.35% is a **machine fact** (the code stripped its percent sign); conflicting model fraction declarations
            # yield to fact, or 0.35 multiplies by 100 and renders as 35%.
            # contradicts_fraction misses this case: it requires column max>1.5, while these values are small.
            logger.info("add_chart.percent_fraction_overridden_by_text col=%s —— 文本带 %%，按 points", fname)
            pct_scale = None
        if unit == "%" and not pct_scale:
            # ★ The code restoring 15.37% to 15.37 is the only stage that **knows** the shape
            # (units.py: human-entered % means values are already scaled, hence points). Previously it set only unit,
            # not shape; frontend classification stage ① saw nothing, stage ② name families did not recognize % itself,
            # leaving stage ③ to guess by whole-column range: **when all values are under 1% (0.35% /0.8% /0.12%),**
            # it guesses decimals and multiplies by 100, displaying 35.0%. Any ≥1% value in the column makes it work,
            # so this appears only when all values are small, eluding previous tests.
            col.format = "percent"
            col.percent_scale = "points"
        if pct_scale:
            # A declared fractional growth rate may legitimately exceed 100%.
            col.format = "percent"
            col.percent_scale = pct_scale
        elif t_override == "numeric" and t == "numeric" and col.percent_scale is None:
            # Explicit numeric is a declaration of an ordinary number, not an omitted shape.
            # Persist the no-percent marker so a percentage-like name cannot trigger another
            # scaling pass. Apply it after positive declarations; later source metadata that
            # confirms a shape retains its existing precedence.
            col.percent_shape_unknown = True
        cols.append(col)

    # _infer_col_type prefers numeric, classifying **integer time indices**
    # (month_num=1..6 / Year=2025) as measures. Reclassify across the table below.
    _retype_time_index_columns(rows, cols, skip=set(overrides))
    return rows, cols


# Entire column name is a time-unit term AND values fall within that unit's valid integer range; require both.
#
# ⚠ Deliberately **anchor entire column names**, excluding compounds (月销量 /月坪效 /本月门店数):
# these are actual measures; observed 本月门店数 values in 116 happen to fall in 1900–2100,
# so range-only classification wrongly rejects them. The name condition prevents false positives and cannot be omitted.
_TIME_INDEX_UNITS: tuple[tuple[re.Pattern[str], int, int], ...] = (
    (re.compile(r"^\s*(年|年份|年度|财年|year|yr|fy|fiscal[_ ]?year)\s*$", re.I), 1900, 2100),
    (re.compile(r"^\s*(月|月份|月份序号|month|month[_ ]?num|month[_ ]?idx|mth)\s*$", re.I), 1, 12),
    (re.compile(r"^\s*(季|季度|quarter|qtr)\s*$", re.I), 1, 4),
    (re.compile(r"^\s*(周|周次|week|wk)\s*$", re.I), 1, 53),
)


def _is_time_index_column(name: str, values: list[Any]) -> bool:
    """这一列是不是「用整数写的时间刻度」（而不是一个度量）."""
    nonnull = [v for v in values if v is not None and v != ""]
    ints = [v for v in nonnull if isinstance(v, int) and not isinstance(v, bool)]
    if not ints or len(ints) != len(nonnull):
        return False
    return any(pat.match(name) and all(lo <= v <= hi for v in ints) for pat, lo, hi in _TIME_INDEX_UNITS)


def _retype_time_index_columns(
    rows: list[dict[str, Any]],
    cols: list[ChartColumn],
    *,
    skip: set[str],
) -> None:
    """把「整数形式的时间刻度」从度量改判成维度（就地改 `cols`）。

    为什么必须改（116 语料 1948 张实测命中 21 张、误杀 0）：`_infer_col_type` 的
    第一条就是 `all(isinstance(v, (int, float))) → numeric`，**在看列名之前**。
    于是 `month_num=1..6`、`Year=2025/2026` 都成了度量，一个根因制造三种症状：

    1. **真指标被裁掉**。`_prune_cols_for_hint` 按 `true_nums[0]`（列序第一个）挑主指标，
       `month_num` 排在 `营业额` 前面就把它顶掉了 —— 116 上
       `2026年1-6月 逐月营业额趋势` 落库只剩 `month:date | month_num:numeric`，
       图上是一条 1→6 的斜线，**标题承诺的营业额根本不在图里**（同形态 3 张）。
    2. **冒出一条垃圾序列**。`Q2 Net Margin: 2026 vs 2025` 的左轴画的是
       `Year=[2026,2026,2026,2025,2025,2025]`，真指标被挤到右轴。
    3. **本该当分组的「年」变不成维度**，于是长表 pivot（前端 `pivotLongSpec`）
       接不住 —— 那几张 `2026 vs 2025 趋势对比` 正是 pivot 的 docstring 点名的例子，
       两年被接成了一条线。

    改判成 date 还是 categorical，**要看表里有没有别的时间维度**：
    `Q2 Monthly Sales` 是 `Month + Year + Sales`，把 `Year` 判成 date 会让它
    按「date 优先」抢走 x 轴（本该是 Month）。所以只有在**没有别的时间维度**时
    才给 date（`月销量趋势` 里的 `月份` 就该是时间轴），否则给 categorical
    ——那样它成为分组维度，两年正好摊成两条线。

    ⚠ 「别的时间维度」不能只数 `type == date`：`Month` 的取值是 `April/May/June`，
    `_looks_like_date` 认不出来，落成 categorical。第一版只看 date 列，当场把
    Q2 那张的 `Year` 判成了 date。所以还要认**名字带时间词的维度列**。
    """

    def _is_time_dim(c: ChartColumn) -> bool:
        if c.type == "date":
            return True
        if c.type not in ("categorical", "geo"):
            return False
        name = str(c.header or c.key)
        lname = name.lower()
        return any(h in name or h in lname for h in _DATE_HINT)

    has_time_dim = any(_is_time_dim(c) for c in cols)
    for c in cols:
        if c.type != "numeric" or c.key in skip:
            continue
        if not _is_time_index_column(str(c.header or c.key), [r.get(c.key) for r in rows]):
            continue
        c.type = "date" if not has_time_dim else "categorical"
        has_time_dim = True
        logger.info("add_chart.time_index_retyped col=%s to=%s", c.key, c.type)


_GEO_HINT = ("省", "省份", "市", "市级", "县", "区", "州", "国家", "地区")
_DATE_HINT = ("日期", "时间", "月", "年", "季", "周", "date", "month", "year", "quarter")


# Strict patterns for values that are themselves dates; deliberately separate from the **loose** _looks_like_date below:
# _looks_like_date is queried only with time terms already present in column names, allowing broader matching
# (float-corrupted labels like 2026.0年1.0月 depend on it; 70 charts in 116 use this).
# These patterns decide **without column-name backing**; loose matching would mistake ordinary categories like 周边商品
# or 第 3 季度促销档 containing time-related characters for time axes. Entire-string anchors are mandatory.
_STRICT_DATE_RE = re.compile(
    r"^("
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"  # 2026-07-21 / 2026/7/21
    r"|\d{4}[-/]\d{1,2}"  # 2026-07
    r"|\d{4}年\d{1,2}月\d{1,2}日"
    r"|\d{4}年\d{1,2}月"
    r"|\d{4}年"
    r"|\d{4}[-/]?Q[1-4]"  # 2026Q1 / 2026-Q1
    r"|\d{4}年第?[1-4]季度?"
    r")$",
)
# Whole-column time-axis ratio threshold, not any(): one date-like value among 100 categories
# must not reclassify the whole column as a time axis.
_STRICT_DATE_MIN_RATIO = 0.8


def _column_values_are_dates(xs: list[Any]) -> bool:
    """整列取值是否**本身**就是日期（不看列名）."""
    sample = xs[:20]
    if len(sample) < 2:  # One coincidental value is insufficient; retain name-based classification
        return False
    hits = sum(1 for v in sample if _STRICT_DATE_RE.match(str(v).strip()))
    return hits / len(sample) >= _STRICT_DATE_MIN_RATIO


def _infer_col_type(name: str, samples: list[Any]) -> str:
    """结合列名和采样值判断列类型.

    优先级：numeric（全数值）→ date（名含时间词 + 格式匹配）→
    date（**值本身就是日期**，不要求列名背书）→
    geo（名含地理词，且不是数值）→ categorical（兜底）.
    """
    # Take nonempty samples.
    xs = [v for v in samples if v is not None and v != ""]
    if not xs:
        return "categorical"
    # All numeric → numeric.
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in xs):
        return "numeric"
    # Time term in name + most samples match date format → date.
    lname = name.lower()
    if any(h in name or h in lname for h in _DATE_HINT) and any(_looks_like_date(str(v)) for v in xs[:10]):
        return "date"
    # Values take precedence: no time term in name but entire column contains dates → still a time axis.
    #
    # Why needed (116 live window): dimension named 每天 has standard 2026-07-21 values,
    # but _DATE_HINT omits 天. The previous name-AND-value check never examines values
    # after a name miss; ten of 14 missed dates followed this pattern, none yielding line charts
    # (last-30-day refund-profit share trend became a horizontal bar chart).
    #
    # Deliberately do not add 天/日 to _DATE_HINT: vocabulary patches miss later names like 统计日
    # or 业务日期, while 天 matches 天津/聊天 and 日 matches 日本/生日,
    # shifting false positives elsewhere. Value shape is stronger than names, requiring no name backing.
    if _column_values_are_dates(xs):
        return "date"
    # Geographic term in name → geo.
    if any(h in name for h in _GEO_HINT):
        return "geo"
    return "categorical"


def _looks_like_date(s: str) -> bool:
    if not s:
        return False
    if "年" in s or "月" in s or "季" in s or "周" in s:
        return True
    for fmt in ("%Y-%m", "%Y-%m-%d", "%Y/%m", "%Y/%m/%d", "%Y-Q%q"):
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False
