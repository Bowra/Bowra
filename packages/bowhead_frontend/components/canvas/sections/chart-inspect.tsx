"use client";

/**
 * Batch 2 §3.1 · Chart inspection layer — focus room (design B v2).
 *
 * Role (PRD §2): charts in reports stay quiet and readable; users seeking detail enter here to
 * examine charts **without changing the question or rerunning analysis**. Every operation is
 * a "cheap BI action" — purely frontend-derived (applyView), with zero requests for 97.2% of charts; changing questions uses "追问这张图" to return to chat.
 *
 * No flag (user decision 2026-08-12): enabled by default directly; rollback = previous image.
 *
 * view-transforms and ref-line-plugin guarantee honesty: Top-N's "其余" sets ratio/mean-type
 * columns to null instead of summing; reference lines use visible series only, with scoped labels for truncated views.
 */

import { ChartTableUnits } from "./chart-table-units";
import { qualifyTableUnitText, tableColumnDisplays } from "@/lib/chart/table-display";
import { analyticalValue } from "@/lib/chart/analytical";
import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { Tab, TabList, Tabs } from "@/components/ui/tabs";
import { useChartTheme } from "@/lib/chart-theme";
import { formatObservedValue, measureDisplayName, resolvePercentColumn, resolvePercentScale, formatObservedPercentCell, axisUnitOf } from "@/lib/chart/format";
import { chartDatasetAvailable, evidencePlanForViewer } from "@/lib/chart/presentation-plan";
import { observationRows, type ObservationAnnotation, type ObservationTarget } from "@/lib/chart/annotations";
import type { FocusBinding } from "@/lib/chart/linked-focus";
import { FULL_ROWS_LIMIT, applyView, ensureFullRows, isViewTruncated, type ChartViewState } from "@/lib/chart/view-transforms";
import { toggleSeriesKey } from "@/lib/chart/series-visibility";
import type { ChartSpec } from "@/lib/chart/types";
import { snapshotWithAllPointLabels } from "@/lib/chart/capture-canvas";
import { canCopyImage, copyCanvasToClipboard } from "@/lib/chart/copy-image";
import { ChartTimeWindowContext } from "@/lib/chart/time-window-context";
import { ChartFocusContext } from "@/lib/chart/focus-context";
import { selectedContextForView } from "@/lib/chart/selected-context";
import { requestChartFollowup } from "@/lib/chart/chart-followup";
import { exportChartCSV, exportChartPNG } from "@/lib/chart/export";
import { track } from "@/lib/metrics";

import { Dialog, DialogShell } from "@/components/ui/dialog";
import { useNarrowViewport } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { ChartSourceLine } from "./chart-source-line";

import { ChartBlock } from "./chart-block";
import { CharIcon } from "@/components/char-icon";

const TOP_N_CHOICES = [5, 10, 20, 50] as const;
/** Chart-kind candidates for the "视图" chip. Unlike ChartBlock's _compatibleKinds, which recommends
 *  by data shape, this means "view from another angle": offer all kinds and let unsuitable ones fall back
 *  to a table (showing why a kind is unsuitable is more honest than withholding the option). */
const VIEW_KINDS: ChartSpec["kind"][] = [
  "bar", "hbar", "line", "area_stacked", "stacked_bar",
  "dual_axis", "combo", "pie", "scatter", "waterfall", "table",
];
/** Only chart kinds with Cartesian value axes can draw reference lines. pie/donut have no scales, so plugin
 *  can only return early — giving them a checkbox creates an inert switch (caught in batch 2 device acceptance). */
const REF_LINE_KINDS = new Set<ChartSpec["kind"]>([
  "bar", "hbar", "line", "area_stacked", "stacked_bar", "dual_axis", "combo", "scatter", "waterfall",
]);

export interface ChartInspectProps {
  spec: ChartSpec;
  /** Attached caption (feeds protagonist parsing, maintaining the same emphasis as the chart in the report). */
  captionText?: string | null;
  onClose: () => void;
  /** Element to receive focus after closing. */
  returnFocusRef?: React.RefObject<HTMLElement | null>;
}

export function ChartInspect({ spec, captionText, onClose, returnFocusRef }: ChartInspectProps) {
  const focus = useContext(ChartFocusContext);
  const scope = useContext(ChartTimeWindowContext);
  const t = useTranslations("chart_inspect");
  const unitText = useTranslations("chart_table");
  const locale = useLocale();
  const tBlock = useTranslations("chart_block");
  const theme = useChartTheme();
  const [tab, setTab] = useState<"chart" | "data">("chart");
  const [kind, setKind] = useState<ChartSpec["kind"] | null>(null);
  const [view, setView] = useState<ChartViewState>({});
  const [showRefLine, setShowRefLine] = useState(false);
  const [fullSpec, setFullSpec] = useState<ChartSpec | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [copyHint, setCopyHint] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Check copy capability once after mount: SSR has no window, and production on 116 uses plain HTTP (not a secure
  // context) → ClipboardItem does not exist; the entry must degrade rather than do nothing when clicked.
  const [copySupported, setCopySupported] = useState(false);
  useEffect(() => setCopySupported(canCopyImage()), []);

  // Complete rows on entry: sorting/Top-N over partial data misrepresents it (37 charts had rows < row_count).
  useEffect(() => {
    let canceled = false;
    const have = (spec.rows ?? []).length;
    const total = spec.row_count ?? have;
    if (have >= total || !spec.data_artifact_id) return;
    // Available rows already reach the maximum completion can return (spec can contain 10,000 rows after full completion,
    // while `/preview` returns at most `FULL_ROWS_LIMIT` rows) ⇒ fetching would only return fewer.
    // Skip fetching, **without treating it as failure**: footer still shows "手里 N / 共 M", not a false "加载失败".
    if (have >= FULL_ROWS_LIMIT) return;
    // ★ Deliberately **do not block on** `series_shown` here (main-chart path is guarded by `needsRowCompletion`).
    //   The different criteria are intentional; the difference is **who requested it**:
    //     · Main chart renders passively — users did not request 59 series; restoring them bypasses backend readability gate;
    //     · Users **actively enter** inspection; release notes promise "要看全部分组就来这里".
    //   This layer has its own footer ("显示 N / 共 M 行") and filters; users can narrow an unreadable view themselves.
    //   ⚠ Before changing this, update corresponding `complete-chart-rows.ts` comments and release notes to keep them aligned.
    setLoading(true);
    void ensureFullRows(spec)
      .then((result) => {
        if (canceled) return;
        setFullSpec(result.ok ? result.spec : null);
        setLoadFailed(!result.ok);
      })
      .finally(() => {
        if (!canceled) setLoading(false);
      });
    return () => {
      canceled = true;
    };
  }, [spec]);

  const baseSpec = chartDatasetAvailable(spec) ? fullSpec ?? spec : spec;
  const analytical = Boolean(baseSpec._export_analytical);
  const restLabel = t("rest_bucket");
  // Fewer available rows than total = `ensureFullRows` did not complete (artifact GC / 403 / limit exceeded).
  // ⚠ Calculate **before** `derived`: `applyView` needs this input to decide whether "其余" gets a subtotal.
  const partial = (baseSpec.rows ?? []).length < (baseSpec.row_count ?? 0);
  const derived = useMemo(() => {
    if (analytical) return baseSpec;
    // review R4-6: restLabel is required, otherwise category in "其余" row renders sentinel `__rest__`
    // — in the chart, data-tab table and downloaded CSV alike.
    const next = applyView(baseSpec, view, { restLabel, partial });
    // Design B1 "视图" chip: kind switching belongs in control row (chart inside layer is pure chart without its own switcher).
    return kind ? ({ ...next, kind } as ChartSpec) : next;
  }, [baseSpec, view, kind, restLabel, partial, analytical]);
  // Chart-tab spec **keeps columns**: chart applies dataset `hidden` according to `hiddenSeries`
  // (single implementation shared by both entry points; see `series-visibility.ts`). Data tab and CSV still use
  // `derived` — tables have no hidden-dataset concept; hidden series simply do not appear as columns.
  //
  // ★ This makes the rule against changing chart kind when hiding a line **structurally guaranteed**, beyond test coverage:
  //   Numeric columns no longer disappear, so dual_axis/combo/area_stacked shape requirements always hold;
  //   therefore P2 (116 device R8) automatic compatible-kind downgrade + kind_degraded hint path
  //   disappears along with its trigger — it was only a patch for the column-removal implementation.
  //   If users explicitly select an unsuitable kind in View, still use fallback table per VIEW_KINDS decision.
  const chartSpec = useMemo(() => {
    if (analytical) return baseSpec;
    const next = applyView(baseSpec, { ...view, hiddenSeries: undefined }, { restLabel, partial });
    return kind ? ({ ...next, kind } as ChartSpec) : next;
  }, [baseSpec, view, kind, restLabel, partial, analytical]);
  const numericCols = useMemo(
    () => (baseSpec.columns ?? []).filter((c) => c.type === "numeric"),
    [baseSpec.columns],
  );
  // Fewer available rows than total → every aggregation (including reference-line means) must state its scope
  // review R4-5: `partial` reflects only **fetch** truncation. View truncation also makes an unqualified mean
  // misleading: with all 98 rows available and Top-5 selected, reference line averages 5 bars plus one rest
  // aggregate bucket, yet label merely says "均值". ref-line-plugin docstring already
  // required callers to pass partial: true after Top-N truncation; implementation had not connected it.
  //
  // R5-5: criterion uses **actual applyView output** (`isViewTruncated` pure function, guarded there),
  // rather than whether user clicked Top-N — Top-10 on an 8-row chart truncates nothing; adding a scope suffix
  // would incorrectly cast doubt on an honest number.
  const refLinePartial = partial || isViewTruncated(baseSpec, derived);

  const setViewPatch = useCallback((patch: Partial<ChartViewState>, action: string) => {
    track("bowhead_chart_inspect_total", { action });
    setView((prev) => ({ ...prev, ...patch }));
  }, []);

  // Both entries (in-layer legend and control-row chips) enter here and write the same `view.hiddenSeries`.
  // Toggle semantics reside in `series-visibility.ts`, no longer duplicated per entry.
  const toggleSeries = useCallback((key: string) => {
    track("bowhead_chart_inspect_total", { action: "toggle_series" });
    setView((prev) => ({ ...prev, hiddenSeries: toggleSeriesKey(prev.hiddenSeries, key) }));
  }, []);

  const canvasEl = () => bodyRef.current?.querySelector("canvas") ?? null;

  const onDownloadPng = () => {
    track("bowhead_chart_inspect_total", { action: "download_png" });
    const el = canvasEl();
    if (el) exportChartPNG(el, scope ? `${spec.title || "chart"}_${scope.filenameSuffix}` : spec.title || "chart", scope?.label);
  };

  const onCopyImage = async () => {
    track("bowhead_chart_inspect_total", { action: "copy_image" });
    const el = canvasEl();
    if (!el) return;
    // The copied image has no tooltip: take it with every point label kept.
    const result = await copyCanvasToClipboard(snapshotWithAllPointLabels(el));
    // Failures must be shown — this sprint specifically eliminates silent failures
    setCopyHint(result.ok ? t("copy_done") : t(`copy_failed_${result.reason}`));
    window.setTimeout(() => setCopyHint(null), 4000);
  };

  const onFollowup = () => {
    scope?.beforeCapture?.();
    track("bowhead_chart_inspect_total", { action: "followup" });
    requestChartFollowup({
      title: spec.title,
      selectedContext: isViewTruncated(baseSpec, derived) && !focus?.selection ? undefined : selectedContextForView(scope?.followup?.scope ?? focus?.followupScope, scope?.followup?.binding ?? focus?.binding, focus?.selection ?? null, derived) ?? undefined,
      dataArtifactId: spec.data_artifact_id ?? null,
      kind: spec.kind,
      captionText: captionText ?? null,
    });
    onClose();
  };

  const title = tab === "data" || chartSpec.kind === "table"
    ? qualifyTableUnitText(spec.title, tableColumnDisplays(derived.columns, derived.rows ?? [], locale).values(), (unit) => unitText("source_unit_label", { unit }))
    : spec.title;

  return (
    <DialogShell
      presentation="center"
      onClose={onClose}
      label={title}
      returnFocusRef={returnFocusRef}
      backdropClassName="rc-inspect-backdrop"
      panelClassName="rc-inspect-panel"
    >
      <header className="rc-inspect-head">
        <div className="rc-inspect-title" style={{ color: theme.ink }}>{title}</div>
        <Tabs
          value={tab}
          onValueChange={(next) => { setTab(next); track("bowhead_chart_inspect_total", { action: `tab_${next}` }); }}
        >
          <TabList appearance="segmented" className="rc-inspect-seg">
            <Tab value="chart" className={tab === "chart" ? "is-on" : undefined}>{t("tab_chart")}</Tab>
            <Tab value="data" className={tab === "data" ? "is-on" : undefined}>{t("tab_data")}</Tab>
          </TabList>
        </Tabs>
        <button
          type="button"
          className="rc-inspect-close"
          data-dialog-initial-focus=""
          aria-label={t("close")}
          onClick={onClose}
        ><CharIcon ch="✕" /></button>
      </header>

      {scope ? <p className="px-5 text-xs text-muted-foreground">{scope.label}</p> : null}
      {/* Controls remain in both tabs: sorting/Top-N/series toggles belong to the **derived view**;
          chart and data are two representations of that view, so switching tabs must preserve user settings.
          Value labels and reference lines are chart-specific, appearing only in chart tab. */}
      <div className="rc-inspect-ctrls">
          <label className="rc-inspect-chip">
            <span>{t("view")}</span>
            <select
              value={kind ?? baseSpec.kind}
              onChange={(e) => {
                setKind(e.target.value as ChartSpec["kind"]);
                track("bowhead_chart_inspect_total", { action: "view_kind" });
              }}
            >
              {(analytical ? [baseSpec.kind] : VIEW_KINDS).map((k) => (
                <option key={k} value={k}>{tBlock(`kind.${k}`)}</option>
              ))}
            </select>
          </label>

          {/* **Disable sorting** with incomplete data. Available rows are backend's first N by primary measure:
              ascending order cannot show the true smallest rows (those are absent),
              and sorting another column cannot show that column's top N. `ensureFullRows` comments already said
              fake Top10 from partial data is precisely what this sprint eliminates, but control was never disabled.
              Keep Top-N enabled: first 5 of first N are still genuinely first 5. */}
          <label className={`rc-inspect-chip${partial ? " is-disabled" : ""}`} title={partial ? t("agg_needs_full_data") : undefined}>
            <span>{t("sort")}</span>
            <select
              disabled={partial || analytical}
              value={view.sort ? `${view.sort.key}:${view.sort.dir}` : ""}
              onChange={(e) => {
                const raw = e.target.value;
                if (!raw) return setViewPatch({ sort: undefined }, "sort_clear");
                const [key, dir] = raw.split(":");
                setViewPatch({ sort: { key: key!, dir: dir as "asc" | "desc" } }, "sort");
              }}
            >
              <option value="">{t("sort_none")}</option>
              {numericCols.map((c) => (
                <optgroup key={c.key} label={c.header}>
                  <option value={`${c.key}:desc`}>{c.header} ↓</option>
                  <option value={`${c.key}:asc`}>{c.header} ↑</option>
                </optgroup>
              ))}
            </select>
          </label>

          <label className="rc-inspect-chip">
            <span>{t("top_n")}</span>
            <select
              disabled={analytical}
              value={view.topN ?? ""}
              onChange={(e) => setViewPatch(
                { topN: e.target.value ? Number(e.target.value) : undefined },
                "top_n",
              )}
            >
              <option value="">{t("top_n_all")}</option>
              {TOP_N_CHOICES.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>

          {tab === "chart" && !analytical ? (
            <>
              <label className="rc-inspect-chip">
                <span>{t("data_labels")}</span>
                <input
                  type="checkbox"
                  checked={view.showLabels ?? false}
                  onChange={(e) => setViewPatch({ showLabels: e.target.checked }, "data_labels")}
                />
              </label>
              {REF_LINE_KINDS.has(chartSpec.kind) ? (
                // **Disable means** when full rows cannot be fetched: mean of 30 rows is not mean of all 40 rows;
                // a "基于已加载部分" suffix cannot rescue it — users read line position, not suffix.
                // Compute only when accurate; otherwise omit (user 2026-09-02: "还不如不要聚合值算 subtotal").
                <label className={`rc-inspect-chip${partial ? " is-disabled" : ""}`} title={partial ? t("agg_needs_full_data") : undefined}>
                  <span>{t("ref_line")}</span>
                  <input
                    type="checkbox"
                    checked={showRefLine && !partial}
                    disabled={partial}
                    onChange={(e) => {
                      setShowRefLine(e.target.checked);
                      track("bowhead_chart_inspect_total", { action: "ref_line" });
                    }}
                  />
                </label>
              ) : null}
            </>
          ) : null}

          <span className="rc-inspect-spacer" />

          {!analytical && numericCols.length > 1 ? numericCols.map((c) => {
            const hidden = (view.hiddenSeries ?? []).includes(c.key);
            return (
              <button
                key={c.key}
                type="button"
                className={`rc-inspect-series${hidden ? " is-off" : ""}`}
                aria-pressed={!hidden}
                onClick={() => toggleSeries(c.key)}
              >{c.header}</button>
            );
          }) : null}
      </div>

      <div className="rc-inspect-body" ref={bodyRef} aria-busy={loading || undefined}>
        {tab === "chart" ? (
          <ChartBlock
            spec={chartSpec}
            captionText={captionText}
            hiddenSeries={view.hiddenSeries}
            onToggleSeries={toggleSeries}
            refLines={showRefLine ? [{
              kind: "mean",
              label: t("ref_line_mean"),
              partial: refLinePartial,
              // The two truncation scopes need different wording; fetch truncation is more severe and takes priority
              partialNote: partial ? t("ref_line_partial") : t("ref_line_view_partial"),
            }] : undefined}
            chromeless
          />
        ) : (
          <InspectDataTable spec={derived} />
        )}
      </div>

      <footer className="rc-inspect-foot" style={{ color: theme.inkSoft }}>
        <span>
          {t("rows_shown", {
            shown: (derived.rows ?? []).length,
            total: baseSpec.row_count ?? (baseSpec.rows ?? []).length,
          })}
          {partial ? ` · ${t("partial_note")}` : ""}
          {loadFailed ? ` · ${t("load_failed")}` : ""}
          {copyHint ? ` · ${copyHint}` : ""}
        </span>
        <span className="rc-inspect-actions">
          {tab === "chart" ? (
            <button type="button" className="v2-chart-action-btn" onClick={onDownloadPng}>
              {t("download_png")}
            </button>
          ) : null}
          <button
            type="button"
            className="v2-chart-action-btn"
            onClick={() => {
              track("bowhead_chart_inspect_total", { action: "download_csv" });
              exportChartCSV(derived, scope ? `${spec.title || "chart"}_${scope.filenameSuffix}` : spec.title || "chart");
            }}
          >
            {t("download_csv")}
          </button>
          {tab === "chart" ? (
            copySupported ? (
              <button type="button" className="v2-chart-action-btn" onClick={onCopyImage}>
                {t("copy_image")}
              </button>
            ) : (
              // Degrade: plain HTTP / old browsers lack clipboard API — offer equivalent capability and explain why,
              // rather than leave an inert button (tech-design §3.4).
              //
              // R8 (116 device): design originally named fallback "下载 PNG", but it was **right beside**
              // actual "下载 PNG" button: visible labels were identical, distinguishable only by title,
              // which requires hover. Change to wording that includes reason (user decision to revise design 2026-08-14);
              // all three designs are synchronized.
              <button
                type="button"
                className="v2-chart-action-btn"
                title={t("copy_unavailable_hint")}
                onClick={onDownloadPng}
              >
                {t("copy_degraded_download")}
              </button>
            )
          ) : null}
          <button type="button" className="v2-chart-action-btn is-primary" onClick={onFollowup}>
            {t("followup")}
          </button>
        </span>
      </footer>
    </DialogShell>
  );
}

/** Data-tab table: same conventions as report tables (column-level percentage mode + precision mode). */
function InspectDataTable({ spec }: { spec: ChartSpec }) {
  const theme = useChartTheme();
  const locale = useLocale();
  const columnDisplays = useMemo(() => tableColumnDisplays(spec.columns ?? [], spec.rows ?? [], locale), [spec.columns, spec.rows, locale]);

  return (
    <>
    <ChartTableUnits columns={[...columnDisplays.values()]} />
    <table className="v2-chart-table-el">
      <thead>
        <tr>
          {(spec.columns ?? []).map((c) => (
            <th key={c.key} className={c.type === "numeric" ? "is-num" : undefined} style={{ color: theme.inkSoft }}>
              {columnDisplays.get(c.key)?.header ?? c.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {(spec.rows ?? []).map((r, i) => (
          <tr key={i}>
            {(spec.columns ?? []).map((c) => {
              const v = r[c.key];
              const numeric = c.type === "numeric";
              const display = numeric
                ? columnDisplays.get(c.key)!.formatValue(v)
                : String(v ?? "—");
              return (
                <td key={c.key} className={numeric ? "is-num" : undefined} style={{ color: theme.ink }}>
                  {display}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
    </>
  );
}

/** Compact original-cell inspection, sharing the existing source, dialog and follow-up owners. */
export function ChartObservationDetails({ spec, target, binding, annotation, onClose, returnFocusRef }: {
  spec: ChartSpec;
  target: ObservationTarget;
  binding: FocusBinding;
  annotation?: ObservationAnnotation;
  onClose: () => void;
  returnFocusRef?: React.RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("chart_block");
  const tInspect = useTranslations("chart_inspect");
  const focus = useContext(ChartFocusContext);
  const narrow = useNarrowViewport(true);
  const rows = observationRows(target, spec, binding) ?? [];
  const plan = evidencePlanForViewer(spec.presentation_plan);
  const relevantNotes = plan?.evidence_notes.filter((note) => annotation?.note_ids ? annotation.note_ids.includes(note.id) : note.measure_keys.includes(target.measureKey));
  const label = target.kind === "point" ? target.tuple.map((value) => value ?? "—").join(" · ") : `${target.lower} – ${target.upper}`;
  const selectedContext = target.kind === "point" ? selectedContextForView(focus?.followupScope, binding, focus?.selection ?? null, { ...spec, rows }) : null;
  const locale = useLocale();
  const analyticalKeys = spec._export_analytical ? new Set([...binding.measure_keys, ...(spec._export_analytical.detail_keys ?? []), ...(spec._export_analytical.summary_keys ?? []), ...[spec._export_analytical.range?.coverage_key, spec._export_analytical.range?.origin_key].filter((key): key is string => !!key)]) : null;
  const columns = spec.columns.filter((column) => analyticalKeys ? analyticalKeys.has(column.key) : column.type === "numeric" && binding.measure_keys.includes(column.key));
  const shown = target.kind === "point" ? rows : rows.filter((row) => row[binding.observation_keys[0]] === target.lower || row[binding.observation_keys[0]] === target.upper);
  const content = <>
    {annotation?.label ? <p className="mb-3 text-sm break-words">{annotation.label}</p> : null}
    {target.kind === "interval" ? <p className="mb-3 text-xs text-muted-foreground">{t("observation_endpoints")}</p> : null}
    {shown.map((row, index) => <div key={index} className="mb-3">
      {target.kind === "interval" ? <p className="mb-1 text-xs font-medium">{String(row[binding.observation_keys[0]])}</p> : null}
      <dl className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
        {columns.map((column) => {
          const raw = row[column.key];
          const value = typeof raw === "string" || (typeof raw === "number" && Number.isFinite(raw)) ? raw : null;
          const display = analyticalKeys ? analyticalValue(spec, column.key, row[column.key], locale) : resolvePercentColumn(column)
            ? formatObservedPercentCell(value, resolvePercentScale((spec.rows ?? []).map((item) => item[column.key]), column), column.display)
            : formatObservedValue(value, axisUnitOf(column), column.format, true, column.display, column.currency_code);
          return <div key={column.key} className="contents"><dt className="min-w-0 break-words text-muted-foreground">{measureDisplayName(column)}</dt><dd className="min-w-0 break-words text-right tabular-nums">{display}</dd></div>;
        })}
      </dl>
    </div>)}
    {spec.source_meta || plan?.evidence_notes.length ? <div className="mt-3">
      <p className="text-xs text-muted-foreground">{t(annotation?.note_ids ? "observation_cited_context" : "observation_measure_context")}</p>
      <ChartSourceLine source={annotation?.note_ids ? null : spec.source_meta ?? null} notes={relevantNotes} measureLabels={Object.fromEntries(spec.columns.map((column) => [column.key, column.header]))} />
    </div> : null}
    {selectedContext && spec.data_artifact_id ? <Button variant="plain" size="sm" className="mt-2" onClick={() => {
      requestChartFollowup({ title: spec.title, dataArtifactId: spec.data_artifact_id!, kind: spec.kind, selectedContext });
      onClose();
    }}>{tInspect("followup")}</Button> : null}
  </>;
  if (narrow) return <Dialog title={label} onClose={onClose} returnFocusRef={returnFocusRef} presentation="sheet">
    <div data-chart-observation-details className="p-4" onClick={(event) => event.stopPropagation()}>{content}</div>
  </Dialog>;
  return <section data-chart-observation-details aria-label={t("observation_details", { value: label })}
    className="mt-3 max-w-lg border-t border-border pt-3" onMouseDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()}
    onKeyDown={(event) => { event.stopPropagation(); if (event.key === "Escape") { event.preventDefault(); onClose(); } }}>
    <div className="mb-2 flex items-start justify-between gap-3"><h4 className="min-w-0 break-words text-sm font-medium" style={{ margin: 0 }}>{label}</h4>
      <Button variant="plain" size="sm" onClick={onClose}>{t("observation_close")}</Button></div>
    {content}
  </section>;
}
