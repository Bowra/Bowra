"use client";

/**
 * Chart-Table — chart system table fallback.
 *
 * Triggers:
 *   1. spec.kind === "table" (infer decides fallback)
 *   2. ErrorBoundary falls back here when other kinds fail rendering
 *
 * Batch 3 4.1 (chart-table-ux): replace pagination with collapse. Default first COLLAPSED_ROWS + "展开全部
 * N 行"; expansion uses ensureFullRows to complete limit_rows-truncated data (37 production charts
 * have fewer rows than row_count — old pagination used partial data but footer /98, misrepresenting total).
 * Expanded mode provides vertical scroll container (required for `th{position:sticky}`, previously dead code).
 * Collapsed mode disables sorting: sorting only 10 available rows falsely implies full-table Top10.
 */

import { useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Button } from "@/components/ui/button";
import { groupedEntries, intervalColumn } from "@/lib/chart/analytical";
import { ChartIntervalCell, ChartIntervalLegend } from "./chart-interval-cell";
import { buildTrendTable, trendGeometry } from "@/lib/chart/table-trends";
import { tupleKey } from "@/lib/chart/linked-focus";
import { ChartFocusContext } from "@/lib/chart/focus-context";
import { ChartTimeWindowContext } from "@/lib/chart/time-window-context";
import { focusTuple, sameFocus } from "@/lib/chart/linked-focus";
import { annotationCaption, annotationMatchesRow } from "@/lib/chart/annotations";
import { useVirtualizer } from "@tanstack/react-virtual";
import ReactMarkdown from "react-markdown";
import { useLocale, useTranslations } from "next-intl";

import { useCanvasContext } from "@/lib/canvas-pm-adapter/canvas-context";
import { useChartTheme } from "@/lib/chart-theme";
import { exportChartCSV } from "@/lib/chart/export";
import {
  formatDateLabel,
  formatGrainLabel,
  formatObservedPercentCell,
  formatObservedValue,
  comparePreparedNumbers,
  axisUnitOf,
  quantityUnitOf,
  resolvePercentColumn,
  tableHeaderWithUnit,
  resolvePercentScale,
  type PercentScale,
} from "@/lib/chart/format";
import { chartDatasetAvailable } from "@/lib/chart/presentation-plan";
import { TABLE_FULL_ROWS_LIMIT, ensureFullRows } from "@/lib/chart/view-transforms";
import type { ChartSpec } from "@/lib/chart/types";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { appPath } from "@/lib/base-path";
import { track } from "@/lib/metrics";
import { formatExactSourceNumber } from "@/lib/source-content";
import { TableActions } from "./table-actions";
import { CharIcon } from "@/components/char-icon";

/** Collapsed row count = production p50 (discussion in tech-design §4.1). */
const COLLAPSED_ROWS = 10;

/** Export single-table render limit. Fixed 1600×900 export page uses overflow:hidden; after header/footer,
 *  ~33px rows fit just over 20; more rows get hard-cropped (even footer scope vanishes).
 *  25 rows + explicit total footer is more honest than cropping half of full data or silently rendering 10.
 *  Users obtain complete data through "下载 CSV". */
const EXPORT_MAX_ROWS = 25;

/** Smallest zoom a too-wide export table is fitted to, so a table up to ~1.65x the frame width fits whole. Shrinking
 *  further would trade cropped columns for unreadable ones; wider tables keep the faded edge as the honest marker. */
const EXPORT_MIN_ZOOM = 0.6;

/** Enable virtual scrolling only above this count. Direct rendering simpler for small tables, avoiding dynamic-height edge cases;
 *  200-row DOM cost negligible; benefit (avoiding tens of thousands of `<tr>`) exists only for large tables. */
const VIRTUALIZE_MIN_ROWS = 200;
/** Estimated row height: real-browser single row measured 36px (`/spike/chart-audit?only=table-big&rows=5000`,
 *  1440 wide · light). Underestimating makes scrollbar grow later, so use measured value.
 *  **First-screen** estimate only — `td` lacks `nowrap`; text wraps, actual heights measured by `measureElement`. */
const ROW_H_ESTIMATE = 36;

type SortDir = "asc" | "desc";
interface SortState { key: string; dir: SortDir }

export function ChartTable({
  spec, switcher, surface, sectionId, chromeless, presentationRootId,
  cellMarkdown, sandboxBadge, downloadFilename, exactNumbers,
}: {
  spec: ChartSpec;
  switcher?: React.ReactNode;
  surface?: "canvas" | "chat" | "export";
  sectionId?: string;
  presentationRootId?: string;
  /** Batch 2 §3.1: no own header in inspection (title at layer top, actions at bottom).
   *  review R4-8: ChartBlock `chromeless` previously reached only ChartJsBlock branch;
   *  "table" belongs to VIEW_KINDS — selecting "表格" in layer rendered
   *  duplicate title + open data/download CSV here, precisely what this prop removes. */
  chromeless?: boolean;
  /** Next three apply only to `data_table` sections (see `lib/chart/data-table-to-spec.ts`).
   *  All **opt-in**: omitted stays byte-for-byte as before, affecting none of 438 existing chart tables. */
  /** Render cell as inline markdown (`**粗**` / `*斜*` / `` `码` ``).
   *  ⚠ Never default-enable for every table: ordinary cells containing `*` would be interpreted. */
  cellMarkdown?: boolean;
  /** 🐍 Sandbox badge. */
  sandboxBadge?: boolean;
  /** With original filename, "下载" retrieves original file rather than CSV export. */
  downloadFilename?: string | null;
  /** Opt-in for evidence views (citation source drawer): numeric cells keep every decimal the source
   *  printed and only group thousands. Precision mode rounds 857.83 to 858, which reads as a
   *  contradiction when the reader is checking a report figure against its source. */
  exactNumbers?: boolean;
}) {
  const t = useTranslations("chart_table");
  const originalFocus = useContext(ChartFocusContext);
  const [trendRaw, setTrendRaw] = useState(false);
  const [trendEntity, setTrendEntity] = useState<string | null>(null);
  const windowScope = useContext(ChartTimeWindowContext);
  // datarc provides no en-US labels for fixed time-grain headers (`每月`/`每季度`);
  // leave other headers unchanged — product names/business dimensions have no English equivalents; translation would fabricate them.
  const locale = useLocale();
  const theme = useChartTheme();
  const [sort, setSort] = useState<SortState | null>(null);
  const { registerPresentationOverride } = useCanvasContext();
  const restoreTrend = useCallback(() => { setTrendRaw(false); setTrendEntity(null); setSort(null); }, []);
  useEffect(() => {
    const owner = presentationRootId ?? sectionId;
    if (!owner || (!spec._export_table_trend && !spec._export_analytical) || (!trendRaw && !sort) || !registerPresentationOverride) return;
    return registerPresentationOverride(owner, restoreTrend);
  }, [presentationRootId, sectionId, spec._export_table_trend, spec._export_analytical, trendRaw, sort, registerPresentationOverride, restoreTrend]);
  // ☠☠ **User intent** and **data availability** are distinct, previously squeezed into one `expanded`.
  //
  // Component lacks "收起" button (expansion button disappears after use), so `expanded` can revert true →
  // false **only through reset effect below**. Reset is necessary: changed spec content retaining
  // earlier fetched rows renders stale data. Thus each content change also discarded user intent
  // to see all rows, appearing as expansion followed by automatic collapse.
  //
  // Criterion had three versions (see specSignature comments below), each blocking only current
  // trigger; another trigger revived symptom — customer report 2026-09-08, then user again 2026-09-10:
  // "有时候刚展开就收起来，有时候过段时间收起". Two timings correspond to two rerender classes,
  // both same disease: **discarding intent along with data**.
  //
  // Separating them removes rerender guessing: intent survives content changes; discard stale data, then automatically refetch.
  const [expandIntent, setExpandIntent] = useState(false);
  const [expanding, setExpanding] = useState(false);
  const [fullSpec, setFullSpec] = useState<ChartSpec | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  // Genuine expansion = user wants it **and** full data available. Preserve invariant: no completed data means not expanded,
  // otherwise partial collapsed data enables sorting, producing fake Top10 (fixed in R1).
  const expanded = expandIntent && fullSpec !== null;

  const sourceSpec = chartDatasetAvailable(spec) ? fullSpec ?? spec : spec;
  const trend = useMemo(() => chartDatasetAvailable(spec) ? buildTrendTable(sourceSpec, { start: t("trend_start"), end: t("trend_end"), change: t("trend_change"), trend: t("trend_series"), points: t("trend_points") }) : null, [sourceSpec, spec, t]);
  const summary = trend && (!trendRaw || surface === "export") ? trend : null;
  const rawRows = trendEntity && trend && surface !== "export" ? sourceSpec.rows?.filter((row) => tupleKey([row[spec._export_table_trend!.binding.entity_keys[0]]]) === trendEntity) : null;
  const interval = sourceSpec._export_analytical?.preset === "interval_rows" ? sourceSpec._export_analytical : undefined;
  const plotColumn = useMemo(() => interval ? intervalColumn(sourceSpec, t("analytical_interval")) : null, [sourceSpec, interval, t]);
  const activeSpec = summary?.spec ?? (rawRows ? { ...sourceSpec, rows: rawRows, row_count: rawRows.length } : sourceSpec);
  const visibleColumns = useMemo(() => interval && plotColumn
    ? [...interval.row_keys!.map((key) => activeSpec.columns.find((c) => c.key === key)!), plotColumn, ...(interval.detail_keys ?? []).filter((key) => !interval.row_keys!.includes(key)).map((key) => activeSpec.columns.find((c) => c.key === key)!)]
    : activeSpec.columns, [interval, plotColumn, activeSpec.columns]);
  const plotHeader = useRef<HTMLTableCellElement>(null);
  const [plotWidth, setPlotWidth] = useState(320);
  useLayoutEffect(() => {
    const element = plotHeader.current;
    if (!element || !interval) return;
    const measure = () => setPlotWidth(Math.max(320, element.clientWidth - 24));
    measure();
    const observer = new ResizeObserver(measure); observer.observe(element);
    return () => observer.disconnect();
  }, [interval]);
  const focus = summary ? null : originalFocus;
  useEffect(() => { setTrendEntity(null); setTrendRaw(false); }, [spec._export_table_trend, spec.presentation_plan]);
  const rows = useMemo(() => {
    const copy = [...(activeSpec.rows ?? [])];
    if (!sort) return interval?.group_key ? groupedEntries(copy, interval).flatMap((entry) => entry.row ? [entry.row] : []) : copy;
    const col = activeSpec.columns.find((c) => c.key === sort.key);
    const isNum = col?.type === "numeric";
    copy.sort((a, b) => {
      const av = a[sort.key], bv = b[sort.key];
      if (av === bv) return 0;
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      const cmp = isNum
        ? comparePreparedNumbers(av, bv)
        : String(av).localeCompare(String(bv));
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return interval?.group_key ? groupedEntries(copy, interval).flatMap((entry) => entry.row ? [entry.row] : []) : copy;
  }, [activeSpec.rows, activeSpec.columns, sort, interval]);

  // review P3: when spec changes (ChatInlineChart / same-artifact streaming update without keyed remount),
  // discard previous chart's fetched rows, otherwise render previous data.
  //
  // ☠ Criterion had three versions; two produced user-visible bugs:
  //   R4-7  signature `data_artifact_id|title|row_count|rows.length` — too shallow:
  //         ①same artifact streams changed values without changed count → no reset → old data remains;
  //         ②inspection hides series, changing only columns → no reset → toggle does nothing.
  //   R5-3  revert to **spec object identity**, relying on all callers passing memoized specs
  //         as **repository-wide invariant**, **guarded by nothing**:
  //         any caller (`insight-card` fresh chartLookup result, inspection,
  //         future surfaces) passing new object clears expansion, appearing as
  //         "点了展开全部，随后自己收起来" (exact customer-reported shape 2026-09-08).
  //
  // Now compare **content signature**, including both R4-7 omissions (all column attributes + actual row values):
  // semantically equivalent to identity criterion (genuine change always resets), but immune to `{...spec}`
  // rerenders replacing only shell, without relying on unenforceable invariant.
  //
  // ⚠ Cost: serialize once per spec **object** change; watch prop (inline data,
  // usually ≤50 rows), not completed fullSpec, so constant-scale overhead.
  const specSignature = useMemo(() => {
    const cols = spec.columns ?? [];
    const colSig = cols
      .map((c) => [c.key, c.header, c.type, c.format ?? "", c.unit ?? "", String(c.percent_scale ?? "")].join("\u0001"))
      .join("\u0002");
    const rowSig = (spec.rows ?? [])
      .map((r) => cols.map((c) => String(r[c.key] ?? "")).join("\u0001"))
      .join("\u0002");
    return [spec.kind, spec.data_artifact_id ?? "", String(chartDatasetAvailable(spec)), String(spec.row_count ?? 0), colSig, rowSig, JSON.stringify(spec._export_analytical ?? null)].join("\u0003");
  }, [spec]);

  // Content changed ⇒ discard **data-derived** state (fetched rows, truncation/failure flags, sorting).
  // ⚠ Deliberately **preserve `expandIntent`**: user intent survives data changes.
  // Effect below automatically restores discarded data.
  useEffect(() => {
    setFullSpec(null);
    setTruncated(false);
    setLoadFailed(false);
    setSort(null);
  }, [specSignature]);

  // review R0-2: percentage scale is **column property** — per-cell detection for [0.83, 13.5] renders
  // 83.0% / 13.5% (100-fold difference; 48 actual production columns mixed this way). Determine mode once per column.
  const percentScales = useMemo(() => {
    const out = new Map<string, PercentScale>();
    for (const c of activeSpec.columns ?? []) {
      if (c.type !== "numeric" || !resolvePercentColumn(c)) continue;
      out.set(c.key, resolvePercentScale((activeSpec.rows ?? []).map((r) => r[c.key]), c));
    }
    return out;
  }, [activeSpec.columns, activeSpec.rows]);

  const totalRows = activeSpec.row_count ?? rows.length;
  // Export never collapses (PDF has no clickable "展开全部"), but cannot pretend complete: render only
  // what fits, with explicit total in footer.
  const isExport = surface === "export";
  // review R5-7: chromeless (inspection table) also never collapses. Layer already completed data
  // and derived selected-view rows; cutting back to 10 while "展开全部" disappears with footer
  // causes unexplained silent truncation, precisely what this sprint eliminates.
  // Layer has its own scroll container, so many rows are fine.
  const showAllRows = isExport || chromeless;
  const visibleRows = isExport
    ? rows.slice(0, interval ? 10 : EXPORT_MAX_ROWS)
    : (showAllRows || expanded ? rows : rows.slice(0, COLLAPSED_ROWS));
  const entries = useMemo(() => groupedEntries(visibleRows, interval), [visibleRows, interval]);
  const collapsible = !showAllRows && totalRows > COLLAPSED_ROWS;

  // ── Virtual scrolling ──────────────────────────────────────────────
  // Enable only with **actual scroll container**: `.v2-chart-table-scroll` gets `max-height + overflow-y:auto`
  // only with `is-expanded`; without scrolling ancestor virtualization cannot measure viewport,
  // silently rendering first screen only and losing rows. Use same expression as class condition below.
  //
  // **Never** virtualize export: screenshot captures one frame, yielding half-table there.
  // ⚠ `!isExport` is currently **redundant** (`scrollable` already false on export,
  // visibleRows capped at `EXPORT_MAX_ROWS`=25 < threshold), so mutating it does not fail tests
  // — verified. Retain because other changes may remove either premise (export limit above
  // 200, or expansion entry on export); failure is half-table PDF.
  const scrollRef = useRef<HTMLDivElement>(null);
  const scrollable = expanded || (chromeless && !isExport);
  const virtualize = scrollable && !isExport && visibleRows.length > VIRTUALIZE_MIN_ROWS;
  const rowVirtualizer = useVirtualizer({
    count: virtualize ? entries.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_H_ESTIMATE,
    overscan: 12,
  });
  const virtualItems = virtualize ? rowVirtualizer.getVirtualItems() : [];
  // ★ fail-open: virtualization enabled but no visible items calculable (zero-height container / missing `is-expanded`
  // CSS / ref unattached in first frame): **render all rows** rather than empty table.
  // Performance degradation merely feels briefly slow; empty table means missing data.
  const useVirtual = virtualize && virtualItems.length > 0;
  const padTop = useVirtual ? virtualItems[0]!.start : 0;
  const padBottom = useVirtual
    ? rowVirtualizer.getTotalSize() - virtualItems[virtualItems.length - 1]!.end
    : 0;

  // Horizontal overflow must remain visible: clipping a numeric cell can resemble a complete value.
  // Screen and export surfaces need different treatments:
  //   - screen: the first (identity) column stays put (sticky, CSS) and the edge with hidden columns fades out
  //     (`has-more-*`), so a cut number never looks whole and the row label never scrolls away;
  //   - export: a screenshot cannot scroll, so overflow was silently cropped. Zoom the table down to fit the frame,
  //     no further than `EXPORT_MIN_ZOOM`; anything still wider keeps the fade as the honest marker.
  const [edges, setEdges] = useState({ left: false, right: false });
  // Export only: columns still past the frame after the minimum zoom. A screenshot cannot scroll, so they are named
  // in a note under the table rather than cut off silently.
  const [hiddenColumns, setHiddenColumns] = useState(0);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const sync = () => {
      const left = el.scrollLeft > 1;
      const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
      setEdges((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
    };
    if (isExport) {
      const table = el.querySelector("table");
      if (table) {
        table.style.zoom = "";
        const ratio = el.clientWidth / table.scrollWidth;
        const zoom = ratio < 1 ? Math.max(EXPORT_MIN_ZOOM, ratio) : 1;
        // Imperative on purpose: it must be undone before measuring, and the <table> has no React-owned style.
        table.style.zoom = zoom === 1 ? "" : String(zoom);
        const frameRight = el.getBoundingClientRect().right;
        const cut = [...table.querySelectorAll("thead th")].filter(
          (th) => th.getBoundingClientRect().right > frameRight + 0.5,
        ).length;
        setHiddenColumns((prev) => (prev === cut ? prev : cut));
      }
    }
    sync();
    el.addEventListener("scroll", sync, { passive: true });
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(sync) : null;
    observer?.observe(el);
    return () => {
      el.removeEventListener("scroll", sync);
      observer?.disconnect();
    };
  }, [isExport, visibleRows.length, activeSpec.columns]);

  // review R1-29: export previously **never called ensureFullRows** (only interactive expansion did);
  // 98-row table showed only 50 available rows in PDF, without hint. Auto-complete once here;
  // set aria-busy during fetch — export ready criterion already waits for [aria-busy] disappearance
  // (visual/page.tsx:88), so screenshot waits for data.
  const [exportFetching, setExportFetching] = useState(false);
  useEffect(() => {
    if (!isExport) return;
    const have = (spec.rows ?? []).length;
    const total = spec.row_count ?? have;
    if (have >= total || !spec.data_artifact_id) return;
    let canceled = false;
    setExportFetching(true);
    void ensureFullRows(spec)
      .then((result) => {
        if (canceled) return;
        setFullSpec(result.spec);
        setTruncated(result.truncated);
        setLoadFailed(!result.ok);
      })
      .finally(() => {
        if (!canceled) setExportFetching(false);
      });
    return () => {
      canceled = true;
    };
    // As above: **content signature**, not object identity. Both effects in this file need same criterion,
    // otherwise one avoids reset but other still refetches rows (exact waste described by old comments).
    // Body uses current render's `spec`; equal signature means equal content, making either equivalent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isExport, specSignature]);

  const expand = useCallback(async () => {
    track("bowhead_chart_action_total", { action: "expand_table", kind: "table" });
    if (fullSpec && !loadFailed) {
      setExpandIntent(true);
      return;
    }
    setExpanding(true);
    try {
      // "展开全部" uses **table** limit (virtual scrolling removes DOM row limits).
      // Export still uses default chart limit — only `EXPORT_MAX_ROWS` render; fetching extra 1.2MB
      // wastes resources and slows pre-screenshot aria-busy wait.
      const result = await ensureFullRows(spec, { maxRows: TABLE_FULL_ROWS_LIMIT });
      setFullSpec(result.spec);
      setTruncated(result.truncated);
      // review R1: only actual data completion counts as expanded. Original unconditionally
      // setExpanded(true) in finally, hiding button and enabling sorting after fetch failure (shared report 403),
      // letting users produce fake Top10 from partial data — exactly what component comments promise to prevent.
      // Clear intent on fetch failure too, otherwise completion effect below retries indefinitely.
      // After `expand_failed`, user can click again — **new** expression of intent.
      setExpandIntent(result.ok);
      setLoadFailed(!result.ok);
    } finally {
      setExpanding(false);
    }
  }, [spec, fullSpec, loadFailed]);

  // Intent remains but reset cleared data ⇒ auto-complete, without another user click.
  // All three guards needed: `expanding` prevents concurrent reentry; `loadFailed` prevents endless retries
  // (failure already clears `expandIntent`; double protection); nonnull `fullSpec` needs no completion.
  useEffect(() => {
    if (expandIntent && fullSpec === null && !expanding && !loadFailed) {
      void expand();
    }
  }, [expandIntent, fullSpec, expanding, loadFailed, expand]);

  const download = () => {
    windowScope?.beforeCapture?.();
    // Batch 0 0-F: table download action previously had zero telemetry
    track("bowhead_chart_action_total", { action: "download_csv", kind: "table" });
    if (summary) {
      exportChartCSV({ ...activeSpec, rows: activeSpec.rows?.map((row) => ({ ...row, __trend_series: summary.series.get(String(row.__trend_series))!.labels.map((key, index) => `${key}: ${summary.series.get(String(row.__trend_series))!.values[index] ?? "—"}`).join("; ") })) }, `${spec.title || "chart"}${windowScope ? "_" + windowScope.filenameSuffix : ""}`);
    } else if (windowScope) {
      exportChartCSV(activeSpec, `${spec.title || "chart"}_${windowScope.filenameSuffix}`);
    } else if (spec.data_artifact_id && chartDatasetAvailable(spec)) {
      // Long tables directly download original artifact CSV
      window.open(
        appPath(`/api/v1/files/${encodeURIComponent(spec.data_artifact_id)}`),
        "_blank",
      );
    } else {
      exportChartCSV(activeSpec, spec.title || "chart");
    }
  };

  const actions = [
    ...(trend ? [{ key: "trend-view", label: t(summary ? "trend_show_rows" : "trend_show_summary"), onClick: () => { setTrendRaw(!!summary); setTrendEntity(null); setSort(null); } }] : []),
    {
      key: "open-data",
      label: t("open_data"),
      onClick: () => {
        track("bowhead_chart_action_total", { action: "open_data", kind: "table" });
        requestCanvasDataFocus(spec.data_artifact_id!, {
          entryPoint: surface === "chat" ? "chat" : "report",
          reportContext: surface === "chat"
            ? undefined
            : { label: spec.title, sectionId },
        });
      },
      hidden: !spec.data_artifact_id,
    },
    // With original filename on `data_table`, download uses **original file link** (old `DataTableSection`
    // was `<a href>` with `⬇ filename`), not a newly exported CSV.
    downloadFilename && spec.data_artifact_id && chartDatasetAvailable(spec)
      ? {
          key: "download-file",
          label: `⬇ ${downloadFilename}`,
          href: appPath(`/api/v1/files/${encodeURIComponent(spec.data_artifact_id)}`),
        }
      : { key: "download-csv", label: t("download_csv"), onClick: download },
  ];

  // No collapsed sorting: only first N available; true maximum may be absent.
  //
  // review R6-2: chromeless makes collapsible always false; `!collapsible || expanded` therefore
  // **enables** header-click sorting, but layer data can be partial (failed ensureFullRows leaves
  // baseSpec at 50 of 98), producing false maximum; after Top-N, table also has rest
  // "其余" aggregate bucket, even more absurd to sort alongside genuine categories. Explanatory footer
  // (sort_needs_expand) is hidden too, leaving no hint.
  // Layer has its own sorting dropdown (applyView derives from full data); disable header sorting in layer outright.
  const sortable = !chromeless && (!collapsible || expanded || !!interval);

  const onHeaderClick = (key: string) => {
    // ★ Criterion defined only once in `sortable`. First R6-2 version changed only it (header styling/aria use it),
    //   while this handler retained duplicated old condition, still sorting on header click in layer —
    //   new guard caught it immediately. Same criterion must not have second implementation.
    if (!sortable) return;
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, dir: "desc" };
      return { key, dir: prev.dir === "desc" ? "asc" : "desc" };
    });
  };

  const scaleNotes = !isExport && !exactNumbers ? sourceSpec.columns.flatMap((column) => {
    if (column.type !== "numeric" || percentScales.has(column.key)) return [];
    const quantity = quantityUnitOf(column);
    return quantity.exponent !== 0 && quantity.base
      ? [t("source_scale", { measure: formatGrainLabel(column.header, locale), source: axisUnitOf(column)!, displayed: quantity.base })]
      : [];
  }) : [];

  return (
    <div
      className="v2-chart-table"
      role="region"
      aria-label={spec.title}
      data-chart-chrome={chromeless ? "none" : undefined}
      aria-busy={exportFetching || undefined}
    >
      {chromeless ? null : (
        <div className="v2-chart-header">
          <div className="v2-chart-title" style={{ color: theme.ink }}>{spec.title}</div>
          {spec.subtitle ? (
            <div className="v2-chart-subtitle" style={{ color: theme.inkSoft }}>{spec.subtitle}</div>
          ) : null}
          {sandboxBadge ? <span className="v2-dt-badge"><CharIcon ch="🐍" /> Python Sandbox</span> : null}
          <TableActions
              surface={surface}
              variant="quiet"
              moreLabel={t("more_actions")}
              actions={actions}
            >{switcher}</TableActions>
        </div>
      )}
      {interval ? <ChartIntervalLegend spec={sourceSpec} profile={interval} /> : null}
      {trend ? <div className="mb-2 text-xs text-muted-foreground" data-chart-trend-scope>
        {summary ? t("trend_scope", { measure: spec.columns.find((column) => column.key === spec._export_table_trend!.measure_key)?.header ?? "", start: summary.series.values().next().value?.labels[0] ?? "", end: summary.series.values().next().value?.labels.at(-1) ?? "" }) : <Button variant="plain" size="sm" onMouseDown={(event) => { event.preventDefault(); event.stopPropagation(); }} onClick={(event) => { event.stopPropagation(); event.currentTarget.focus({ preventScroll: true }); setTrendRaw(false); setTrendEntity(null); setSort(null); }}>{t("trend_show_summary")}</Button>}
      </div> : null}
      {/* review R6-5: chromeless rendered all rows, but `expanded` stayed false,
          omitting `is-expanded` — that rule (globals.css) provides vertical scrolling, and
          `th{position:sticky}` **requires scroll ancestor**. Long inspection table lost header
          after two screens (same failure fixed in batch 3 4.1); DOM also laid out thousands of rows.
          Omit on export (no max-height in PDF); interactive gets it whenever all rows render. */}
      <div
        ref={scrollRef}
        className={`v2-chart-table-scroll${scrollable ? " is-expanded" : ""}${virtualize ? " is-virtual" : ""}${edges.left ? " has-more-left" : ""}${edges.right ? " has-more-right" : ""}`}
      >
        {/* Virtualization leaves only one screen of `<tr>` in DOM, making screen-reader row count wrong.
            `aria-rowcount` / `aria-rowindex` are designed for this: tell assistive technology
            total rows and current row number. Header is 1, data starts at 2.
            Omit without virtualization — DOM already complete, adding creates another count to synchronize. */}
        {/* `is-virtual` + the column count: a virtualized table renders only a window of rows, so content-sized
            columns would change width while scrolling. The report canvas keeps it on fixed layout with a stable
            per-column minimum width instead (report-canvas.css). */}
        <table
          className="v2-chart-table-el"
          style={virtualize ? ({ "--chart-table-columns": visibleColumns.length } as CSSProperties) : undefined}
          {...(useVirtual ? { "aria-rowcount": entries.length + 1 } : {})}
        >
          <thead>
            <tr {...(useVirtual ? { "aria-rowindex": 1 } : {})}>
              {visibleColumns.map((c) => {
                const active = sort?.key === c.key;
                const arrow = active ? (sort!.dir === "desc" ? " ↓" : " ↑") : "";
                const numeric = c.type === "numeric";
                return (
                  <th
                    key={c.key}
                    ref={c.key === plotColumn?.key ? plotHeader : undefined}
                    onMouseDown={(event) => { if (sortable) { event.preventDefault(); event.stopPropagation(); } }}
                    tabIndex={sortable && (!summary || c.key !== "__trend_series") && c.key !== plotColumn?.key ? 0 : undefined}
                    onClick={(event) => { event.stopPropagation(); if ((c.key !== "__trend_series" || !summary) && c.key !== plotColumn?.key) { event.currentTarget.focus({ preventScroll: true }); onHeaderClick(c.key); } }}
                    onKeyDown={(event) => { if ((event.key === "Enter" || event.key === " ") && (c.key !== "__trend_series" || !summary) && c.key !== plotColumn?.key) { event.preventDefault(); event.stopPropagation(); onHeaderClick(c.key); } }}
                    className={numeric ? "is-num" : undefined}
                    aria-sort={active ? (sort!.dir === "desc" ? "descending" : "ascending") : undefined}
                    style={{
                      ...(interval ? { width: c.key === plotColumn?.key ? "60%" : undefined, minWidth: c.key === plotColumn?.key ? 344 : 100 } : {}),
                      color: theme.inkSoft,
                      borderColor: theme.grid,
                      cursor: sortable && (!summary || c.key !== "__trend_series") && c.key !== plotColumn?.key ? "pointer" : "default",
                    }}
                  >
                    {c.key === plotColumn?.key && interval ? <ChartIntervalCell spec={sourceSpec} width={plotWidth} profile={interval} /> : numeric && !exactNumbers && !percentScales.has(c.key) ? tableHeaderWithUnit(formatGrainLabel(c.header, locale), axisUnitOf(c)) : formatGrainLabel(c.header, locale)}{arrow ? <>{" "}<CharIcon ch={arrow.trim()} /></> : ""}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {/* Virtual-scroll top/bottom spacer rows: fill unrendered height, making scrollbar length accurate.
                `colSpan` must follow column count or this row distorts table layout. */}
            {padTop > 0 ? (
              <tr aria-hidden style={{ height: padTop }}>
                <td colSpan={visibleColumns.length} style={{ padding: 0, border: "none" }} />
              </tr>
            ) : null}
            {(useVirtual ? virtualItems.map((vi) => [entries[vi.index]!, vi.index] as const)
              : entries.map((entry, i) => [entry, i] as const)
            ).map(([entry, i]) => {
              const r = entry.row;
              if (!r) return <tr key={entry.key} {...(useVirtual ? { "data-index": i, "aria-rowindex": i + 2, ref: rowVirtualizer.measureElement } : {})} data-chart-interval-group>
                <th colSpan={visibleColumns.length} scope="rowgroup" style={{ textAlign: "left", paddingTop: 18, color: theme.ink, borderColor: theme.grid }}><span style={{ position: "sticky", left: 12 }}>{String(entry.group ?? "—")}</span></th>
              </tr>;
              return (
              // Virtualization requires `data-index` + `measureElement`: `td` lacks nowrap,
              // text wraps ⇒ variable row height, requiring measurement rather than only `estimateSize`.
              <tr
                key={entry.key}
                tabIndex={focus?.select ? 0 : undefined}
                aria-selected={focus ? sameFocus(r, focus.binding, focus.selection) : undefined}
                style={focus && sameFocus(r, focus.binding, focus.selection) ? { outline: `2px solid ${theme.ink}`, outlineOffset: "-2px" } : undefined}
                onClick={focus?.select ? (event) => {
                  event.stopPropagation(); event.currentTarget.focus({ preventScroll: true });
                  const cell = (event.target as HTMLElement).closest("td");
                  const clicked = cell ? visibleColumns[cell.cellIndex] : null;
                  const measure = clicked?.type === "numeric" ? clicked.key : focus.binding.measure_keys[0];
                  focus.select?.(focusTuple(r, focus.binding), { kind: "point", tuple: focus.binding.observation_keys.map((key) => r[key]), measureKey: measure });
                } : undefined}
                onKeyDown={focus?.select ? (event) => {
                  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); focus.select?.(focusTuple(r, focus.binding), { kind: "point", tuple: focus.binding.observation_keys.map((key) => r[key]), measureKey: focus.binding.measure_keys[0] }); }
                  if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (focus.pinned) focus.closeDetails?.(); else focus.select?.(null); }
                } : undefined}
                {...(useVirtual
                  ? { "data-index": i, "aria-rowindex": i + 2, ref: rowVirtualizer.measureElement }
                  : {})}
              >
                {visibleColumns.map((c) => {
                  const v = r[c.key];
                  const annotations = (activeSpec._export_annotations ?? []).filter((item) => item.encoding === activeSpec.kind && item.measure_key === c.key && annotationMatchesRow(item, r));
                  // Ordinary cells share hover precision and base units; source verification keeps available digits.
                  const num = typeof v === "number" ? v : null;
                  const numeric = c.type === "numeric";
                  const display = numeric
                    ? exactNumbers
                      ? (typeof v === "string" ? v : formatExactSourceNumber(num))
                      : (percentScales.has(c.key)
                        ? formatObservedPercentCell(typeof v === "string" ? v : num, percentScales.get(c.key)!, c.display)
                        : formatObservedValue(typeof v === "string" ? v : num, axisUnitOf(c), c.format, false, c.display, c.currency_code))
                    // review gap fix (2026-09-01): chart x-axis localized, table did not — English users requesting
                    // monthly **table** still saw `2026年1月`. Same data must not diverge across surfaces.
                    : c.type === "date"
                      ? formatDateLabel(String(v ?? ""), locale) || "—"
                      : String(v ?? "—");
                  return (
                    <td
                      key={c.key}
                      className={numeric ? "is-num" : undefined}
                      data-chart-annotation-cell={annotations.length ? annotations.map((item) => item.id).join(" ") : undefined}
                      title={annotations.length ? annotations.map((item) => annotationCaption(item, activeSpec)).filter(Boolean).join("\n") : undefined}
                      style={{ color: theme.ink, borderColor: theme.gridSoft, ...(annotations.length ? { outline: `1px solid ${theme.inkSoft}`, outlineOffset: "-3px" } : {}) }}
                    >
                      {interval && c.key === plotColumn?.key ? <ChartIntervalCell spec={sourceSpec} width={plotWidth} profile={interval} row={r} /> : summary && c.key === "__trend_series" ? (() => {
                        const series = summary.series.get(String(v))!;
                        const geometry = trendGeometry(series, summary.domain);
                        const valueColumn = spec.columns.find((column) => column.key === spec._export_table_trend!.measure_key)!;
                        const description = series.labels.map((period, index) => `${period}: ${resolvePercentColumn(valueColumn) ? formatObservedPercentCell(series.values[index], resolvePercentScale(series.values, valueColumn), valueColumn.display) : formatObservedValue(series.values[index], valueColumn.unit, valueColumn.format, true, valueColumn.display, valueColumn.currency_code)}`).join("; ");
                        const svg = <svg width="112" height="32" viewBox="0 0 112 32" role="img" aria-label={description} style={{ display: "block", color: theme.ink }}><title>{description}</title><path d={geometry.path} fill="none" stroke="currentColor" strokeWidth="1.5" />{geometry.points.map(([x, y], index) => <circle key={index} cx={x} cy={y} r="1.7" fill="currentColor" />)}</svg>;
                        return isExport ? svg : <Button variant="plain" size="sm" aria-label={t("trend_observations", { entity: String(series.entity) })} onMouseDown={(event) => { event.preventDefault(); event.stopPropagation(); }} onClick={(event) => { event.stopPropagation(); event.currentTarget.focus({ preventScroll: true }); setTrendRaw(true); setTrendEntity(String(v)); setSort(null); }}>{svg}</Button>;
                      })() : v !== null && typeof v === "object" && ("delta" in v || "bar" in v)
                        ? <StructuredCell v={v as { delta: string; pos: boolean } | { bar: number; pos: boolean }} />
                        : cellMarkdown && !numeric && typeof v === "string"
                          ? <MarkdownInline text={display} />
                          : !numeric && !isExport
                            ? <span className={`v2-chart-table-text${c.type === "date" ? " is-date" : ""}`}>{display}</span>
                            : display}
                    </td>
                  );
                })}
              </tr>
            ); })}
            {padBottom > 0 ? (
              <tr aria-hidden style={{ height: padBottom }}>
                <td colSpan={visibleColumns.length} style={{ padding: 0, border: "none" }} />
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {scaleNotes.length ? <p className="v2-chart-table-unit-note text-xs text-muted-foreground" data-chart-unit-note>{scaleNotes.join(" · ")}</p> : null}
      {isExport && hiddenColumns > 0 ? (
        <div data-testid="table-hidden-columns" className="v2-chart-table-footer" style={{ color: theme.inkSoft }}>
          {t("hidden_columns", { n: hiddenColumns })}
        </div>
      ) : null}
      {/* review R5-7: chromeless previously hid only header, leaving footer — **harmful** in inspection,
          beyond duplication: layer supplies applyView-derived spec (Top-N=5 gives
          rows=6, row_count still 98), so footer said "显示 1-6 / 共 98 行", contradicting layer's
          own count; clicking "展开全部 98 行" invoked ensureFullRows, restored original 98 rows, silently
          discarding Top-N and "其余" aggregation. Layer owns its row counts and fetching. */}
      {chromeless ? null : (
        <div className="v2-chart-table-footer" style={{ color: theme.inkSoft }}>
          <span>
            {t("showing", { start: visibleRows.length ? 1 : 0, end: visibleRows.length, total: totalRows })}
            {!sortable ? ` · ${t("sort_needs_expand")}` : ""}
            {loadFailed ? ` · ${t("expand_failed")}` : ""}
            {truncated && !loadFailed ? ` · ${t("expand_truncated", { limit: rows.length })}` : ""}
          </span>
          {collapsible ? (
            <button
              type="button"
              className="v2-chart-action-btn"
              disabled={expanding || !chartDatasetAvailable(spec)}
              onClick={() => {
                if (!expanded) { void expand(); return; }
                const root = scrollRef.current?.closest<HTMLElement>(".v2-chart-table");
                const restorePosition = root && root.getBoundingClientRect().top < 0;
                setExpandIntent(false);
                if (scrollRef.current) scrollRef.current.scrollTop = 0;
                if (restorePosition) root.scrollIntoView?.({ block: "start", behavior: "instant" });
              }}
            >
              {expanded ? t("collapse") : expanding ? t("expanding") : t("expand_all", { total: totalRows })}
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
}

/** P3.2 followup #44: cell markdown, moved **verbatim** from `data-table.tsx`.
 *
 *  ⚠ First version added `allowedElements` allowlist without authorization, **swallowing links** — old implementation allowed
 *  `<a>` (with `target=_blank`). Moving capability is not removing it; preserve original.
 *  (Old comment said block forbidden, but code merely omitted `<p>` wrapper without restricting elements; follow code.)
 *
 *  Only with `cellMarkdown` (`data_table` sections); ordinary tables still render plain text. */
function MarkdownInline({ text }: { text: string }) {
  return (
    <ReactMarkdown
      components={{
        p: ({ children }) => <>{children}</>,
        a: ({ children, href }) => (
          <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
        ),
      }}
    >
      {text}
    </ReactMarkdown>
  );
}

/** Two structured `data_table` cells: change chip and bar. Also moved unchanged.
 *  ⚠ Composer currently has no producer (only `export_ir` / `export_pptx` allowlists recognize them),
 *  but `TableCellValue` supports these types; convergence must not casually remove type support. */
function StructuredCell({ v }: { v: { delta: string; pos: boolean } | { bar: number; pos: boolean } }) {
  if ("delta" in v) {
    return <span className={`delta ${v.pos ? "pos" : "neg"}`}>{v.delta}</span>;
  }
  const pct = Math.max(0, Math.min(100, v.bar));
  return (
    <div className="bar-cell">
      <div className="track">
        <div className={`fill${v.pos ? " pos" : ""}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
