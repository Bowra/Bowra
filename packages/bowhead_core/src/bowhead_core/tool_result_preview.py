"""V5 PRD-E2 · Inline tool result preview builders.

Frontend ChatStream renders inline mini preview card for first ok tool result
in a turn. This module provides typed `ToolResultPreview` schema + builders
that emit structured preview from raw tool result.

5 preview kinds: kpi / table / chart_thumbnail / count / text.

Strategy (per Day 0 spike #2): chart preview is **client-side render** — backend
just passes through chart_spec, frontend uses chart.js. No server-side PNG.

PII redaction: regex-redact email/phone/ssn in preview text fields. Tool wrappers
are responsible for filtering result rows to top-N before passing here.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Literal, NotRequired, TypedDict

from bowhead_core.agents._generated.tool_renames import canonical_tool_name

logger = logging.getLogger(__name__)


# ─── Typed schemas ─────────────────────────────────────


class CanvasAnchor(TypedDict):
    """Optional canvas section anchor — frontend uses to render '查看完整版 →' link."""

    canvas_id: str
    section_id: str


class _PreviewBase(TypedDict, total=False):
    """Shared fields across all preview kinds."""

    source_label: str | None
    canvas_anchor: CanvasAnchor | None


class KpiPreview(_PreviewBase):
    kind: Literal["kpi"]
    label: str
    value: str
    raw_value: NotRequired[str | int | float | bool | None]
    delta_pct: float | None
    delta_tone: Literal["pos", "neg", "neu"]


class TablePreview(_PreviewBase):
    kind: Literal["table"]
    columns: list[str]
    rows: list[list[str]]
    typed_rows: NotRequired[list[list[str | int | float | bool | None]]]
    total_rows: int


class ChartThumbnailPreview(_PreviewBase):
    kind: Literal["chart_thumbnail"]
    title: str
    chart_kind: Literal["bar", "line", "pie", "stacked_bar"]
    chart_spec: dict[str, Any]


class CountPreview(_PreviewBase):
    kind: Literal["count"]
    label: str
    value: int
    qualifier: str | None


class TextPreview(_PreviewBase):
    kind: Literal["text"]
    preview: str
    full_length: int


ToolResultPreview = KpiPreview | TablePreview | ChartThumbnailPreview | CountPreview | TextPreview


# ─── PII redaction (mirrors frontend redactSqlPii) ────


_PII_PATTERN = re.compile(
    r"\b(email|phone|mobile|ssn|id_card)\s*=\s*(['\"])[^'\"]+\2",
    re.IGNORECASE,
)


def redact_pii(text: str) -> str:
    """Redact `field='value'` PII patterns. V1 simple regex; complex SQL uncovered."""
    return _PII_PATTERN.sub(lambda m: f"{m.group(1)}={m.group(2)}***{m.group(2)}", text)


def _redact_value(v: Any) -> Any:
    if isinstance(v, str):
        return redact_pii(v)
    return v


def _preview_cell(value: Any) -> str | int | float | bool | None:
    """Retain available typed cells, redact text, and preserve unsafe integer digits."""
    if type(value) is float and not math.isfinite(value):
        return None
    if type(value) is int and abs(value) > 2**53 - 1:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return _redact_value(value)
    return str(_redact_value(str(value)))


# ─── Builders ─────────────────────────────────────────


_TABLE_PREVIEW_ROW_CAP = 5
_TABLE_PREVIEW_THRESHOLD = 50  # > 50 rows → count summary instead of table
_TEXT_PREVIEW_CHAR_CAP = 200

# ─────────────────────────────────────────────────────────────────
# Sprint chat-v3 Phase B — `_pick_top_rows_by_delta`
# ─────────────────────────────────────────────────────────────────
# Heuristic: find first column matching delta patterns (同比 / 环比 / yoy /
# delta / change / pct), sort rows by abs(value) desc, return top-N. Falls
# back to natural order if no delta col detected or values aren't numeric.
#
# Why: design "preview top-3" wants users to see the most impactful rows
# (top movers), not arbitrary natural-order first 3. See:
#   docs/sprints/chat-v3-agent-sections-findings/tech-design-r2.md §3
import re as _re_v3

_DELTA_COL_PATTERNS_V3: tuple[str, ...] = (
    r"同比.*变化率$",
    r"环比.*变化率$",
    r"涨跌",
    r"^delta",
    r"^ratio",
    r"^change",
    r"_pct$",
    r"_yoy$",
    r"_mom$",
)


def _is_delta_col(col_name: str) -> bool:
    name = str(col_name)
    return any(_re_v3.search(pat, name, _re_v3.IGNORECASE) for pat in _DELTA_COL_PATTERNS_V3)


def _find_delta_col_v3(cols: list[str]) -> str | None:
    for c in cols:
        if _is_delta_col(c):
            return c
    return None


def _pick_top_rows_by_delta(
    rows: list[dict[str, Any]],
    cols: list[str],
    *,
    n: int = _TABLE_PREVIEW_ROW_CAP,
) -> list[dict[str, Any]]:
    """Sort by abs(delta_col) desc, return top n. Fallback natural order.

    Numeric coercion is permissive — strings like "-1.28%" / "1.5万" are
    cleaned to floats. Failures fall back to natural order silently.
    """
    if not rows:
        return []
    delta_col = _find_delta_col_v3(cols)
    if delta_col is None:
        return rows[:n]

    # Formatted strings are evidence, not a safe numeric sorting representation.
    if any(value is not None and (type(value) not in (int, float)) for value in (row.get(delta_col) for row in rows)):
        return rows[:n]

    def _as_float(v: Any) -> float:
        return float(v) if v is not None else 0.0

    try:
        sorted_rows = sorted(
            rows,
            key=lambda r: abs(_as_float(r.get(delta_col))),
            reverse=True,
        )
        return sorted_rows[:n]
    except Exception:
        return rows[:n]


def build_sql_preview(
    rows: list[dict[str, Any]] | list[list[Any]],
    *,
    source_label: str | None = None,
    canvas_anchor: CanvasAnchor | None = None,
    label_hint: str | None = None,
) -> ToolResultPreview | None:
    """Build preview from SQL/data result.

    - 1 row × 1 col → kpi
    - 1 row × N col → kpi (use first col)
    - 2..50 rows → table (first 5 rows)
    - > 50 rows → count summary (avoid massive inline preview)
    - empty → None
    """
    if not rows:
        return None

    # Normalize to list[dict]
    norm: list[dict[str, Any]]
    if rows and isinstance(rows[0], dict):
        norm = list(rows)  # type: ignore[arg-type]
    elif rows and isinstance(rows[0], (list, tuple)):
        # list-of-lists: synthesize column names
        norm = [
            {f"col_{i}": v for i, v in enumerate(row)}  # type: ignore[arg-type]
            for row in rows
        ]
    else:
        return None

    n = len(norm)
    if n == 1:
        first = norm[0]
        if len(first) == 1:
            key, val = next(iter(first.items()))
            return KpiPreview(
                kind="kpi",
                label=label_hint or _humanize_label(key),
                value=str(_redact_value(val)),
                raw_value=_preview_cell(val),
                delta_pct=None,
                delta_tone="neu",
                source_label=source_label,
                canvas_anchor=canvas_anchor,
            )
        # Multi-col single row → use first col as KPI
        key, val = next(iter(first.items()))
        return KpiPreview(
            kind="kpi",
            label=label_hint or _humanize_label(key),
            value=str(_redact_value(val)),
            raw_value=_preview_cell(val),
            delta_pct=None,
            delta_tone="neu",
            source_label=source_label,
            canvas_anchor=canvas_anchor,
        )

    if n <= _TABLE_PREVIEW_THRESHOLD:
        cols = list(norm[0].keys())
        # Sprint chat-v3 Phase B: pick top-N by delta column abs (not natural
        # order). Falls back to natural order if no delta col detected.
        # Design: docs/sprints/chat-v3-agent-sections-findings/tech-design-r2.md §3
        top_rows = _pick_top_rows_by_delta(norm, cols, n=_TABLE_PREVIEW_ROW_CAP)
        preview_rows: list[list[str]] = []
        for r in top_rows:
            preview_rows.append([str(_redact_value(r.get(c, ""))) for c in cols])
        return TablePreview(
            kind="table",
            columns=cols,
            rows=preview_rows,
            typed_rows=[[_preview_cell(row.get(column)) for column in cols] for row in top_rows],
            total_rows=n,
            source_label=source_label,
            canvas_anchor=canvas_anchor,
        )

    # > 50 rows
    return CountPreview(
        kind="count",
        label=label_hint or "查询结果",
        value=n,
        qualifier="条",
        source_label=source_label,
        canvas_anchor=canvas_anchor,
    )


def build_chart_preview(
    chart_spec: dict[str, Any],
    *,
    title: str = "",
    chart_kind: str = "bar",
    canvas_anchor: CanvasAnchor | None = None,
    source_label: str | None = None,
) -> ChartThumbnailPreview:
    """Pass-through chart spec for client-side render. No server thumbnail gen."""
    return ChartThumbnailPreview(
        kind="chart_thumbnail",
        title=title or chart_spec.get("title", "图"),
        chart_kind=chart_kind if chart_kind in ("bar", "line", "pie", "stacked_bar") else "bar",  # type: ignore[arg-type]
        chart_spec=chart_spec,
        source_label=source_label,
        canvas_anchor=canvas_anchor,
    )


def build_count_preview(
    *,
    label: str,
    value: int,
    qualifier: str | None = None,
    source_label: str | None = None,
    canvas_anchor: CanvasAnchor | None = None,
) -> CountPreview:
    return CountPreview(
        kind="count",
        label=label,
        value=value,
        qualifier=qualifier,
        source_label=source_label,
        canvas_anchor=canvas_anchor,
    )


def build_text_preview(
    full_text: str,
    *,
    source_label: str | None = None,
    canvas_anchor: CanvasAnchor | None = None,
) -> TextPreview:
    full_length = len(full_text)
    snippet = full_text[:_TEXT_PREVIEW_CHAR_CAP]
    snippet = redact_pii(snippet)
    return TextPreview(
        kind="text",
        preview=snippet,
        full_length=full_length,
        source_label=source_label,
        canvas_anchor=canvas_anchor,
    )


# ─── Auto dispatcher (used by subagent emit_tool_result) ────


def build_preview_for_tool(
    tool_name: str,
    result: Any,
    *,
    canvas_anchor: CanvasAnchor | None = None,
) -> ToolResultPreview | None:
    """Best-effort auto-build preview from tool result based on tool_name + shape.

    Returns None when:
    - tool_name not covered (V1: bi.* / add_chart)
    - result shape unrecognized
    - exception in build (logged as warning, not raised)

    Tool wrappers can also build manually + return preview directly via custom path.
    """
    # C4 convergence (display): **historical turns permanently retain names from before renaming**; tool_invocations /
    # event streams are immutable history. Normalize only when reading; never rewrite. Change the lookup function,
    # not individual mapping entries. Contract: docs/design/工具改名迁移.md.
    if not isinstance(result, dict):
        return None
    tool_name = canonical_tool_name(tool_name)

    try:
        # Sprint chat-v3 Phase B — bi_query (omicron-12 canonical name)
        # Routes by inner result shape: ask_question (metric) → KPI · fetch_rows
        # → table · batch {results:[...]} → take first non-empty sub-result
        # (the multi-row preview most informative for FE finding render).
        # Closes Phase A review P0-A2 (bi_query was falling through dispatcher).
        if tool_name == "bi_query":
            # Batch shape: {"results": [{...}, ...]} from N≥2 query path.
            # Real backend stores rows in `sample_rows` (χ ship convention)
            # but legacy flat may use `rows`. Check both.
            # (0.16.1 hotfix — earlier dispatcher only checked `rows` →
            # preview_payload always null → tool_finding never emitted in
            # prod despite valid data.)
            #
            # λ followup (#715): batch aggregation — combine N sub-result
            # first rows into ONE table (each row = one sub-query). Old
            # behavior took FIRST sub → 1 row → KPI (which FE doesn't render).
            # Per chat-v3 phase_b test ground truth: result kind=table.
            sub_results = result.get("results")
            if isinstance(sub_results, list):
                aggregated: list[dict[str, Any]] = []
                for sub in sub_results:
                    if not isinstance(sub, dict) or not sub.get("ok", True):
                        continue
                    rows = sub.get("sample_rows") or sub.get("rows")
                    if isinstance(rows, list) and rows:
                        # Take first sample row from each sub-result
                        first = rows[0]
                        if isinstance(first, dict):
                            # Prepend query_text col for context
                            qt = sub.get("query_text") or sub.get("_user_hint") or ""
                            agg_row: dict[str, Any] = {"query": qt[:60]} if qt else {}
                            agg_row.update(first)
                            aggregated.append(agg_row)
                if not aggregated:
                    # batch with no rows-bearing sub-result → no preview
                    return None
                if len(aggregated) == 1:
                    # Single sub-result rows-bearing → return as table (not KPI)
                    # by feeding to build_sql_preview which auto-promotes 1-row
                    # → KPI. Force table here by direct construction.
                    cols = list(aggregated[0].keys())
                    return TablePreview(
                        kind="table",
                        columns=cols,
                        rows=[[str(_redact_value(aggregated[0].get(c, ""))) for c in cols]],
                        typed_rows=[[_preview_cell(aggregated[0].get(c)) for c in cols]],
                        total_rows=1,
                        source_label=None,
                        canvas_anchor=canvas_anchor,
                    )
                # ≥2 aggregated rows → build_sql_preview handles top-N by delta
                return build_sql_preview(
                    aggregated,
                    source_label=f"{len(aggregated)} 查询",
                    canvas_anchor=canvas_anchor,
                )
            # Flat shape: dispatch by inner type
            qtype = result.get("type")
            if qtype == "metric":
                metrics = result.get("metrics") or []
                if metrics:
                    m = metrics[0]
                    return KpiPreview(
                        kind="kpi",
                        label=str(m.get("name") or "metric"),
                        value=str(m.get("content") if m.get("content") is not None else m.get("value") if m.get("value") is not None else ""),
                        **({"raw_value": _preview_cell(m["value"])} if "value" in m else {}),
                        delta_pct=None,
                        delta_tone="neu",
                        source_label=None,
                        canvas_anchor=canvas_anchor,
                    )
            # Flat shape — check rows then sample_rows
            flat_rows = result.get("rows") or result.get("sample_rows")
            if isinstance(flat_rows, list):
                return build_sql_preview(
                    flat_rows,
                    source_label=result.get("_user_hint") or _table_from_meta(result),
                    canvas_anchor=canvas_anchor,
                )
            # bi_query with neither rows nor metric → no preview
            return None

        # bi_fetch_rows → rows
        if tool_name in ("bi_fetch_rows", "bi_breakdown") and "rows" in result:
            rows = result.get("rows", [])
            if isinstance(rows, list):
                return build_sql_preview(
                    rows,
                    source_label=result.get("_user_hint") or _table_from_meta(result),
                    canvas_anchor=canvas_anchor,
                )

        # bi_ask_question (single metric / image / unsupported)
        if tool_name == "bi_ask_question":
            qtype = result.get("type")
            if qtype == "metric":
                metrics = result.get("metrics") or []
                if metrics:
                    m = metrics[0]
                    return KpiPreview(
                        kind="kpi",
                        label=str(m.get("name") or "metric"),
                        value=str(m.get("content") if m.get("content") is not None else m.get("value") if m.get("value") is not None else ""),
                        **({"raw_value": _preview_cell(m["value"])} if "value" in m else {}),
                        delta_pct=None,
                        delta_tone="neu",
                        source_label=None,
                        canvas_anchor=canvas_anchor,
                    )
            elif qtype == "rows":
                rows = result.get("rows", [])
                if isinstance(rows, list):
                    return build_sql_preview(
                        rows,
                        source_label=result.get("_user_hint"),
                        canvas_anchor=canvas_anchor,
                    )

        # chat-truth Phase B-BE: python_sandbox → rows OR stdout text preview
        if tool_name == "python_sandbox":
            rows = result.get("rows") or result.get("dataframe")
            if isinstance(rows, list) and rows:
                return build_sql_preview(
                    rows,
                    source_label="sandbox",
                    canvas_anchor=canvas_anchor,
                )
            stdout = result.get("stdout") or result.get("output") or ""
            if isinstance(stdout, str) and stdout.strip():
                return build_text_preview(
                    full_text=stdout,
                    source_label="sandbox stdout",
                    canvas_anchor=canvas_anchor,
                )
            return None

        # chat-truth Phase B-BE: kb_search → hits list as table preview
        if tool_name == "kb_search":
            hits = result.get("hits", [])
            if isinstance(hits, list) and hits:
                return build_sql_preview(
                    hits,
                    source_label="kb_search",
                    canvas_anchor=canvas_anchor,
                )
            return None

        # add_chart → result has chart spec
        if tool_name == "add_chart":
            spec = result.get("spec") or result.get("chart_spec") or {}
            if spec and isinstance(spec, dict):
                return build_chart_preview(
                    chart_spec=spec,
                    title=str(spec.get("title", "")),
                    chart_kind=str(spec.get("kind", "bar")),
                    canvas_anchor=canvas_anchor,
                )

    except Exception as e:
        logger.warning("build_preview_for_tool failed: tool=%s err=%s", tool_name, e)
        return None

    return None


# ─── helpers ───────────────────────────────────────────


def _humanize_label(key: str) -> str:
    """Convert column name like 'total_revenue' → 'Total Revenue'."""
    return key.replace("_", " ").title() if key else "value"


def _table_from_meta(result: dict[str, Any]) -> str | None:
    """Extract source table name from result metadata if available."""
    return result.get("_table") or result.get("table") or None


__all__ = [
    "CanvasAnchor",
    "ChartThumbnailPreview",
    "CountPreview",
    "KpiPreview",
    "TablePreview",
    "TextPreview",
    "ToolResultPreview",
    "build_chart_preview",
    "build_count_preview",
    "build_preview_for_tool",
    "build_sql_preview",
    "build_text_preview",
    "redact_pii",
]
