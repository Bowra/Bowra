"use client";

import { ChartDateEntry } from "./chart-date-entry";

/**
 * ChartBlock — main renderer for canvas `chart` sections.
 *
 * Dispatch by spec.kind:
 *   - KPI: ChartKPI (no Chart.js)
 *   - table: ChartTable (no Chart.js)
 *   - Others: dynamic Chart.js import (avoid SSR failures)
 *
 * ErrorBoundary degrades rendering failures to ChartTable.
 */

import {
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  DoughnutController,
  Filler,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  ScatterController,
  Tooltip,
} from "chart.js";
import DatalabelsPlugin from "chartjs-plugin-datalabels";
import { MatrixController, MatrixElement } from "chartjs-chart-matrix";
import { Flow, SankeyController } from "chartjs-chart-sankey";
import { useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { FloatingPortal } from "@floating-ui/react";
import { Chart as ReactChart } from "react-chartjs-2";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { useChartTheme } from "@/lib/chart-theme";
import { buildChartJSConfig, type BuildOpts, type ChartJSConfig } from "@/lib/chart/build-config";
import {
  abbrTick,
  formatCompactValue,
  formatDisplayValue,
  formatPercentCell,
  formatObservedPercentCell,
  formatPrecise,
  resolvePercentColumn,
  resolvePercentScale,
} from "@/lib/chart/format";
import {
  defaultChartRenderBounds,
  HBAR_AXIS_TITLE_HEIGHT_PX,
  resolveChartRenderRecipe,
  type ChartPlacement,
  type ChartRenderContext,
} from "@/lib/chart/render-context";
import { normalizeChartForRenderingV1 } from "@/lib/chart/normalize-chart-for-rendering";
import { isDrawnChartKind, useCompleteChartRows } from "@/lib/chart/complete-chart-rows";
import type { ResolvedChartPresentationV2 } from "@/lib/chart/chart-style-presentation";
import { checkKindShape, checkKindUnits, type ShapeGap } from "@/lib/chart/kind-shape";
import { notAFlowReason } from "@/lib/chart/flow-shape";
import { fitHbarRowsForExport } from "@/lib/chart/export-fit";
import { toggleSeriesKey, type SeriesKey } from "@/lib/chart/series-visibility";
import { setExportHiddenSeries } from "@/lib/chart/export-hidden-series";
import { createRefLinePlugin, type RefLineSpec } from "@/lib/chart/ref-line-plugin";
import { exportChartCSV, exportChartPNG } from "@/lib/chart/export";
import type { ChartColumn, ChartLayout, ChartSpec } from "@/lib/chart/types";
import { chartDatasetAvailable, evidencePlanForViewer } from "@/lib/chart/presentation-plan";
import { ChartFocusContext } from "@/lib/chart/focus-context";
import { analyticalRangeHit, analyticalSummaries } from "@/lib/chart/analytical-series";
import { analyticalBinding, type AnalyticalView } from "@/lib/chart/analytical";
import { focusHighlightPlugin, focusKinds, observationViewEligible, tupleKey, type FocusInteraction, type FocusSelection, type MarkBinding, type TypedCell } from "@/lib/chart/linked-focus";
import { applyTimeWindow, timeWindowDomain, validTimeRange, type TimeRange } from "@/lib/chart/time-window";
import { prepareFrameViews } from "@/lib/chart/prepared-frames";
import { annotationAtPoint } from "@/lib/chart/annotation-layout";
import { annotationBinding, annotationCaption, annotationTarget, observationRows, type ObservationTarget, type ObservationAnnotation } from "@/lib/chart/annotations";
import { usePreparedFrame } from "@/lib/chart/use-prepared-frame";
import { ChartTimeWindowContext } from "@/lib/chart/time-window-context";
import { ChartSharedScaleContext, sharedDomainsForView } from "@/lib/chart/shared-scales";
import { chartBrowsingIdentity, validChartComposition } from "@/lib/chart/composition";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { useCanvasContext } from "@/lib/canvas-pm-adapter/canvas-context";
import { track } from "@/lib/metrics";
import { appPath } from "@/lib/base-path";

import { ChartInspect, ChartObservationDetails } from "./chart-inspect";
import { ChartKPI } from "./chart-kpi";
import { ChartTable } from "./chart-table";
import { ErrorBoundary } from "./error-boundary";
import { ChartActionsLeadOpenContext, TableActions } from "./table-actions";
import { ChartMenuRootContext } from "./chart-menu-root";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { ArtifactPreview } from "@/components/canvas/artifact-preview";
import { fetchFilePreview, type FilePreview } from "@/lib/api";
import { CharIcon } from "@/components/char-icon";
import { LucideGlyph } from "@/components/icon-refresh";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";

// Register Chart.js components + plugins once at module load. datalabels only when explicitly enabled.
// WP2/C1-P0 (#449): full registerables (~30 components) defeated atomic.tsx
// next/dynamic lazy loading — cold production first chart waited seconds after data arrival. Explicitly register
// actual build-config 4 controllers + 2 scales + 4 elements + 3 plugins,
// reducing chunk ~1/3 and synchronous register() cost.
ChartJS.register(
  LineController, BarController, DoughnutController, ScatterController,
  CategoryScale, LinearScale,
  PointElement, LineElement, BarElement, ArcElement,
  Tooltip, Legend, Filler,
  DatalabelsPlugin,
  // WP2 (design D3): Sankey + heat matrix
  SankeyController, Flow,
  MatrixController, MatrixElement,
);
// datalabels off by default (on demand via options.plugins.datalabels.display)
(ChartJS.defaults.plugins as unknown as Record<string, unknown>).datalabels = { display: false };

// #298 fix: remove localStorage chart_kind_override persistence (#85 / #111 design side effect —
// accidental "table" click permanently showed table after refresh rather than BE-recommended bar/line).
// Use per-session in-memory override (refresh restores BE recommendation; user may switch again).

/**
 * Chart frame by placement (Chart Embed Spec D4). A chart has exactly one frame, and it belongs to the host:
 *  - `report` (default): report sections and chat. The chart draws its own title row, kind switch, inspect and ⋯ —
 *    a plain title row, no tinted band and no rule.
 *  - `embed`: pages, the Agent overview, previews. No title row, no kind switch, no ⋯: the host gives the visible
 *    title (pass `labelledBy` to name the figure by it). `kindSwitch` / `actions` opt back in and render into the
 *    host's `headerSlot`. Truncation notes still show, and the exact-value disclosure is on by default.
 *  - `none`: the inspection layer, exact-value tables and cards that already carry title and actions (today's
 *    `chromeless`): plot or table only.
 */
export type ChartChrome = "report" | "embed" | "none";

export function ChartBlock(props: Parameters<typeof SingleChartBlock>[0] & {
  onViewKindChange?: (viewId: string, kind: ChartSpec["kind"]) => void;
  rootRevision?: number;
}) {
  const { spec } = props;
  const t = useTranslations("chart_block");
  if (spec.presentation_plan && typeof spec.presentation_plan === "object" && "version" in spec.presentation_plan && Number(spec.presentation_plan.version) >= 3 && !validChartComposition(spec)) {
    return <p data-chart-analytical-unavailable className="py-6 text-sm text-muted-foreground">{t("analytical_unavailable")}</p>;
  }
  if (!validChartComposition(spec))
    return (
      <>
        <SingleChartBlock {...props} />
      <ChartPlanNotice spec={spec} />
      <ChartCalculations spec={spec} />
      </>
    );
  return <ComposedChartBlock {...props} />;
}

function ComposedChartBlock(props: Parameters<typeof ChartBlock>[0]) {
  const { spec } = props;
  const t = useTranslations("chart_block");
  const controlId = useId();
  const { registerPresentationOverride } = useCanvasContext();
  const figureRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const root = figureRef.current;
    if (!root || props.surface === "export" || typeof ResizeObserver === "undefined") return;
    let frame: number | undefined;
    const variable = "--chart-peer-header-height";
    const align = () => {
      frame = undefined;
      const heights = new Map<HTMLElement, number>();
      root.querySelectorAll<HTMLElement>('[data-chart-group="grid2"], [data-chart-group="split"]').forEach((group) => {
        const rows = new Map<number, Array<{ header: HTMLElement; naturalHeight: number; contentTop: number }>>();
        for (const view of group.children) {
          if (!(view instanceof HTMLElement) || !view.hasAttribute("data-chart-view")) continue;
          const header = view.querySelector<HTMLElement>(":scope > .v2-chart-figure > .v2-chart-header, :scope > .v2-chart-table > .v2-chart-header");
          const body = header?.parentElement?.querySelector<HTMLElement>(":scope > .v2-chart-canvas-wrap, :scope > .v2-chart-table-scroll");
          if (!header || !body) continue;
          const bounds = header.getBoundingClientRect();
          // Exclude the shared minimum so headings can shrink after content or width changes.
          const naturalHeight = Math.max(0, ...Array.from(header.children, (child) => child.getBoundingClientRect().bottom - bounds.top)) + parseFloat(getComputedStyle(header).paddingBottom);
          const top = view.getBoundingClientRect().top;
          const row = rows.get(Math.round(top)) ?? [];
          row.push({ header, naturalHeight, contentTop: body.getBoundingClientRect().top - top - bounds.height + naturalHeight });
          rows.set(Math.round(top), row);
        }
        for (const row of rows.values()) {
          if (row.length < 2) continue;
          const contentTop = Math.max(...row.map((item) => item.contentTop));
          for (const item of row) heights.set(item.header, Math.ceil(item.naturalHeight + contentTop - item.contentTop));
        }
      });
      root.querySelectorAll<HTMLElement>(".v2-chart-header").forEach((header) => {
        const height = heights.get(header);
        if (height === undefined) header.style.removeProperty(variable);
        else if (header.style.getPropertyValue(variable) !== `${height}px`) header.style.setProperty(variable, `${height}px`);
      });
    };
    const schedule = () => { if (frame === undefined) frame = requestAnimationFrame(align); };
    const resize = new ResizeObserver(schedule);
    const observe = () => {
      resize.disconnect();
      resize.observe(root);
      root.querySelectorAll(".v2-chart-header > *").forEach((element) => resize.observe(element));
      schedule();
    };
    const changes = new MutationObserver(observe);
    changes.observe(root, { childList: true, characterData: true, subtree: true });
    observe();
    return () => {
      changes.disconnect();
      resize.disconnect();
      if (frame !== undefined) cancelAnimationFrame(frame);
      root.querySelectorAll<HTMLElement>(".v2-chart-header").forEach((header) => header.style.removeProperty(variable));
    };
  }, [props.surface]);
  const plan = evidencePlanForViewer(spec.presentation_plan);
  const sharedDomains = useMemo(() => new Map(spec.views!.map((view) => [view.id, sharedDomainsForView(plan?.scale_groups ?? [], view.id)])), [spec.views, plan?.scale_groups]);
  const focusEnabled = !!props.sectionId && Number.isSafeInteger(props.rootRevision) && props.rootRevision! > 0 && plan?.dataset.availability === "available" && props.surface !== "export";
  const epoch = useMemo(() => JSON.stringify([props.sectionId, props.rootRevision, chartBrowsingIdentity(spec)]), [props.sectionId, props.rootRevision, spec]);
  const [local, setLocal] = useState<{ epoch: string; fromView: string; selection: FocusSelection } | null>(null);
  const active = focusEnabled && local?.epoch === epoch ? local : null;
  const [pinnedState, setPinnedState] = useState<{ epoch: string; viewId: string; target: ObservationTarget } | null>(null);
  const pinned = focusEnabled && pinnedState?.epoch === epoch ? pinnedState : null;
  const detailReturnRef = useRef<HTMLElement | null>(null);
  const hasPinnedRef = useRef(false);
  hasPinnedRef.current = !!pinned;
  const closeDetails = useCallback(() => {
    if (!hasPinnedRef.current) return;
    setPinnedState(null);
    if (detailReturnRef.current?.isConnected) detailReturnRef.current.focus({ preventScroll: true });
    else figureRef.current?.focus({ preventScroll: true });
  }, []);
  useEffect(() => { setPinnedState(null); }, [epoch, focusEnabled]);
  const [windowState, setWindowState] = useState<{ epoch: string; range: TimeRange } | null>(null);
  const [guideState, setGuideState] = useState<{ epoch: string; index: number } | null>(null);
  const guideSteps = plan?.guide_steps;
  const guideReadable = !!guideSteps?.length && plan?.dataset.availability === "available" && plan.evidence_notes.every((note) => note.availability === "available") && guideSteps.every((step) => spec.views!.some((view) => view.id === step.view_id));
  const guideEnabled = guideReadable && focusEnabled;
  const guideIndex = guideEnabled && guideState?.epoch === epoch ? guideState.index : 0;
  const guideStep = guideEnabled ? guideSteps![guideIndex] : null;
  useEffect(() => {
    if (!guideStep?.annotation_id || guideState?.epoch !== epoch) return;
    const target = [...(figureRef.current?.querySelectorAll<HTMLElement>("[data-chart-annotation]") ?? [])].find((element) => element.dataset.chartAnnotation === guideStep.annotation_id);
    target?.scrollIntoView({ behavior: "auto", block: "nearest" });
  }, [epoch, guideStep?.annotation_id, guideState]);
  useEffect(() => { if (!guideReadable) setGuideState(null); }, [guideReadable]);
  const windowDeclaration = plan?.interactions?.find((interaction) => interaction.preset === "time_window");
  const windowBinding = windowDeclaration ? plan?.bindings?.[windowDeclaration.binding_id] : undefined;
  const domain = useMemo(() => windowDeclaration && windowBinding?.availability === "available" && windowBinding.kind === "time"
    ? timeWindowDomain(spec.views!.filter((view) => windowDeclaration.view_ids.includes(view.id)).map((view) => view.spec), windowBinding) : null,
  [spec.views, windowDeclaration, windowBinding]);
  const windowEnabled = focusEnabled && !!domain?.length;
  const range = windowEnabled && windowState?.epoch === epoch && validTimeRange(windowState.range, domain!) ? windowState.range : null;
  const player = plan?.interactions?.find((item) => item.preset === "frame_player");
  const playerBinding = player ? plan?.bindings?.[player.binding_id] : undefined;
  const frameBinding = playerBinding?.availability === "available" && playerBinding.kind === "frame" ? playerBinding : null;
  const frame = usePreparedFrame(focusEnabled && !!frameBinding, epoch, frameBinding?.frame_keys ?? [], player?.static_frame, figureRef);
  const restoreBrowsing = useCallback(() => { setWindowState(null); setLocal(null); setPinnedState(null); setGuideState(null); frame.reset(); }, [frame.reset]);
  useEffect(() => {
    if (!props.sectionId || (!range && !active && !pinned && guideIndex === 0 && !frame.changed && !frame.playing) || !registerPresentationOverride) return;
    return registerPresentationOverride(props.sectionId, restoreBrowsing);
  }, [props.sectionId, range, active, pinned, guideIndex, frame.changed, frame.playing, registerPresentationOverride, restoreBrowsing]);
  function navigateGuide(index: number) {
    if (!guideEnabled || index < 0 || index >= guideSteps!.length) return;
    restoreBrowsing();
    setGuideState({ epoch, index });
    const step = guideSteps![index];
    const annotation = plan?.annotations?.find((item): item is ObservationAnnotation => item.availability === "available" && item.id === step.annotation_id && item.view_id === step.view_id);
    if (annotation) {
      detailReturnRef.current = document.activeElement as HTMLElement | null;
      setPinnedState({ epoch, viewId: step.view_id, target: annotationTarget(annotation) });
      if (annotation.kind === "point") setLocal({ epoch, fromView: step.view_id, selection: { bindingId: annotation.binding_id, tuple: annotation.tuple } });
    }
    const target = [...(figureRef.current?.querySelectorAll<HTMLElement>("[data-chart-view]") ?? [])].find((element) => element.dataset.chartView === guideSteps![index].view_id);
    target?.scrollIntoView({ behavior: "auto", block: "nearest" });
  }
  const scope = range ? { label: t("window_scope", { start: domain![range.start], end: domain![range.end] }), filenameSuffix: `${domain![range.start]}_${domain![range.end]}` } : null;
  const views = useMemo(() => new Map(spec.views!.map((view) => [view.id, {
    ...view.spec,
    _export_analytical: plan?.analytical_views?.find((item): item is AnalyticalView => item.availability === "available" && item.view_id === view.id),
    _export_table_trend: plan?.table_trends?.find((item): item is import("@/lib/chart/table-trends").TableTrend => item.availability === "available" && item.view_id === view.id),
    _export_annotations: plan?.annotations?.filter((item): item is ObservationAnnotation => item.availability === "available" && item.view_id === view.id),
    source_meta: spec.source_meta,
    presentation_plan: spec.presentation_plan,
    derived_columns: undefined,
    calculation_missing: undefined,
  }])), [spec]);
  const displayedViews = useMemo(() => !range || !domain || windowBinding?.availability !== "available" || windowBinding.kind !== "time" || !windowDeclaration ? views : new Map([...views].map(([id, child]) => [id,
    windowDeclaration.view_ids.includes(id) ? applyTimeWindow(child, windowBinding, domain, range) : child,
  ])), [views, range, domain, windowBinding, windowDeclaration]);
  const frameViews = useMemo(() => frameBinding && player ? prepareFrameViews(views, frameBinding, player.view_ids) : null, [views, frameBinding, player]);
  const frameEnabled = focusEnabled && !!frameViews;
  const frameKey = frameEnabled ? frame.key : player?.static_frame;
  const frameScope = frameViews && frameKey !== undefined ? { label: t("frame_scope", { value: frameKey }), filenameSuffix: String(frameKey), beforeCapture: frame.pause } : null;
  const focusDeclarations = plan?.interactions?.filter((interaction): interaction is FocusInteraction => interaction.preset === "linked_focus");
  const activeInWindow = active && (!range || active.selection.bindingId !== windowDeclaration?.binding_id || domain!.slice(range.start, range.end + 1).includes(active.selection.tuple[0] as string | number)) ? active : null;
  function render(node: ChartLayout, key: string): React.ReactNode {
    if ("view" in node) {
      const frameChild = frameKey !== undefined ? frameViews?.get(node.view)?.get(tupleKey([frameKey])) : undefined;
      const child = frameChild ?? displayedViews.get(node.view)!;
      if (plan?.analytical_views?.some((item) => item.view_id === node.view && item.availability === "unavailable")) return <p key={key} data-chart-view={node.view} data-chart-analytical-unavailable className="py-6 text-sm text-muted-foreground">{t("analytical_unavailable")}</p>;
      const declaredBid = plan?.view_bindings?.[node.view];
      const bid = declaredBid ?? child._export_analytical?.binding_id ?? child._export_annotations?.[0]?.binding_id;
      const binding = declaredBid ? plan?.bindings?.[declaredBid] : analyticalBinding(child) ?? annotationBinding(child);
      const focusedTargets = activeInWindow ? focusDeclarations?.find((interaction) => interaction.from_view === activeInWindow.fromView)?.to_views ?? [] : [];
      const selection = activeInWindow && activeInWindow.selection.bindingId === bid && (activeInWindow.fromView === node.view || focusedTargets.includes(node.view)) ? activeInWindow.selection : null;
      const childScope = frameChild && frameScope ? {
        ...frameScope,
        ...(focusEnabled && frameBinding && plan ? { followup: {
          binding: frameBinding,
          scope: { version: 1 as const, sectionId: props.sectionId!, sectionRev: props.rootRevision!, viewId: node.view,
            bindingId: bid!, datasetDigest: plan.dataset.content_digest!, frameKey },
        } } : {}),
      } : scope && windowDeclaration?.view_ids.includes(node.view) ? scope : null;
      const context = focusEnabled && binding?.availability === "available" && binding.kind !== "frame" && plan && focusKinds.has(child.kind) ? {
        binding, scopeSource: views.get(node.view), datasetDigest: plan.dataset.content_digest!, selection,
        pinned: pinned?.viewId === node.view ? pinned.target : null,
        closeDetails,
        inspectAnnotation: (annotation: ObservationAnnotation) => {
          if (annotationCaption(annotation, child) === null) return;
          frame.pause();
          detailReturnRef.current = document.activeElement as HTMLElement | null;
          setPinnedState({ epoch, viewId: node.view, target: annotationTarget(annotation) });
          setLocal(annotation.kind === "point" ? { epoch, fromView: node.view, selection: { bindingId: bid!, tuple: annotation.tuple } } : null);
        },
        followupScope: declaredBid || child._export_analytical ? {
          version: 1 as const, sectionId: props.sectionId!, sectionRev: props.rootRevision!, viewId: node.view,
          bindingId: bid!, datasetDigest: plan.dataset.content_digest!,
          ...(childScope && range && domain ? { window: { lower: domain[range.start], upper: domain[range.end] } } : {}),
        } : undefined,
        select: (tuple: TypedCell[] | null, observation?: ObservationTarget) => {
          setPinnedState(null);
          if (tuple === null) { setLocal(null); return; }
          if (tuple.length !== binding.dimension_keys.length || !(child.rows ?? []).some((row) => tupleKey(binding.dimension_keys.map((key) => row[key])) === tupleKey(tuple))) return;
          setLocal({ epoch, fromView: node.view, selection: { bindingId: bid!, tuple } });
          if (observation && observationRows(observation, child, binding)?.length) {
            frame.pause();
            detailReturnRef.current = document.activeElement as HTMLElement | null;
            const matched = child._export_annotations?.find((item) => item.kind === "point" && item.encoding === child.kind && item.measure_key === observation.measureKey && observation.kind === "point" && tupleKey(item.tuple) === tupleKey(observation.tuple));
            setPinnedState({ epoch, viewId: node.view, target: matched ? { ...observation, annotationId: matched.id } : observation });
          }
        },
      } : null;
      return (
        <div key={key} className="min-w-0" data-chart-view={node.view} data-chart-guide-active={guideStep?.view_id === node.view ? "true" : undefined}>
          {childScope ? <p className="mb-2 text-xs text-muted-foreground" data-chart-window-scope>{childScope.label}</p> : null}
          <ChartTimeWindowContext.Provider value={childScope}>
          <ChartSharedScaleContext.Provider value={sharedDomains.get(node.view) ?? null}>
          <ChartFocusContext.Provider value={context}>
          {childScope && !child.rows?.length ? <p className="py-8 text-sm text-muted-foreground">{t("window_empty")}</p> :
          <SingleChartBlock
            {...props}
            spec={child}
            sectionId={undefined}
            presentationRootId={props.sectionId}
            presentationIdentity={epoch}
            detailReturnRef={detailReturnRef}
            onKindChange={props.onViewKindChange ? (kind) => props.onViewKindChange!(node.view, kind) : undefined}
            headerSlot={undefined}
            refLines={undefined}
            frozenPresentation={undefined}
            hiddenSeries={undefined}
            onToggleSeries={undefined}
          />}
          {selection && binding?.availability === "available" && binding.kind !== "frame" && !(child.rows ?? []).some((row) => tupleKey(binding.dimension_keys.map((key) => row[key])) === tupleKey(selection.tuple))
            ? <p className="mt-2 text-xs text-muted-foreground" data-chart-focus-outside>{t("focus_outside_scope")}</p> : null}
          </ChartFocusContext.Provider>
          </ChartSharedScaleContext.Provider>
          </ChartTimeWindowContext.Provider>
        </div>
      );
    }
    const columns =
      node.type === "stack"
        ? "grid-cols-1"
        : node.type === "split"
          ? "grid-cols-1 @min-[760px]:grid-cols-[2fr_1fr]"
          : "grid-cols-1 @min-[760px]:grid-cols-2";
    return (
      <div key={key} className={`grid gap-6 ${columns}`} data-chart-group={node.type}>
        {node.children.map((child, index) => render(child, `${key}-${index}`))}
      </div>
    );
  }
  return (
    <section ref={figureRef} tabIndex={-1} onKeyDown={(event) => {
      if (pinned && event.key === "Escape" && !event.defaultPrevented) { event.preventDefault(); event.stopPropagation(); closeDetails(); }
    }} className="@container min-w-0" aria-label={spec.title} data-chart-composition={props.surface === "export" ? "export" : "screen"}>
      {(props.chrome ?? (props.chromeless ? "none" : "report")) === "report" ? (
        <h3 className="mb-5 text-base font-semibold">{spec.title}</h3>
      ) : null}
      {windowEnabled ? <div className="mb-4 flex flex-wrap items-end gap-3" data-chart-time-window>
        <Field id={`${controlId}-start`} label={t("window_start")}>
          <select id={`${controlId}-start`} className="ui-input" value={range?.start ?? 0} onChange={(event) => { const start = Number(event.target.value); setLocal(null); setPinnedState(null); setWindowState({ epoch, range: { start, end: Math.max(start, range?.end ?? domain!.length - 1) } }); }}>
            {domain!.map((value, index) => <option key={index} value={index}>{value}</option>)}
          </select>
        </Field>
        <Field id={`${controlId}-end`} label={t("window_end")}>
          <select id={`${controlId}-end`} className="ui-input" value={range?.end ?? domain!.length - 1} onChange={(event) => { const end = Number(event.target.value); setLocal(null); setPinnedState(null); setWindowState({ epoch, range: { start: Math.min(end, range?.start ?? 0), end } }); }}>
            {domain!.map((value, index) => <option key={index} value={index}>{value}</option>)}
          </select>
        </Field>
        {range ? <Button variant="plain" size="sm" onClick={() => { setWindowState(null); setLocal(null); setPinnedState(null); }}>{t("window_reset")}</Button> : null}
        <ChartDateEntry key={`${epoch}-window`} domain={domain!} range={range ?? { start: 0, end: domain!.length - 1 }} onApply={(next) => { setLocal(null); setPinnedState(null); setWindowState({ epoch, range: next }); }} />
        <p className="basis-full text-xs text-muted-foreground">{scope?.label ?? t("window_full")} · {t("window_export_scope")}</p>
      </div> : null}
      {render(spec.layout!, "root")}
      {guideEnabled ? <div className="mt-4 flex flex-wrap items-center gap-2" data-chart-guide>
        <p className="basis-full text-sm">{t("guide_step", { current: guideIndex + 1, total: guideSteps!.length })} · {guideStep!.title}</p>
        <Button variant="plain" size="sm" disabled={guideIndex === 0} onClick={(event) => { event.currentTarget.focus({ preventScroll: true }); navigateGuide(guideIndex - 1); }}>{t("guide_previous")}</Button>
        <Button variant="plain" size="sm" disabled={guideIndex === guideSteps!.length - 1} onClick={(event) => { event.currentTarget.focus({ preventScroll: true }); navigateGuide(guideIndex + 1); }}>{t("guide_next")}</Button>
        <span className="text-xs text-muted-foreground">{t("guide_canonical_scope")}</span>
      </div> : guideReadable ? <ol className="mt-4 list-decimal space-y-1 pl-5 text-sm" data-chart-guide-summary>
        {guideSteps!.map((step, index) => <li key={index}>{step.title}</li>)}
      </ol> : null}
      {frameEnabled && frameBinding ? <div className="mt-4 flex flex-wrap items-end gap-2" data-chart-frame-player>
        <Field id={`${controlId}-frame`} label={t("frame_label")}>
          <select id={`${controlId}-frame`} className="ui-input" value={frame.index} onChange={(event) => { setPinnedState(null); frame.seek(Number(event.target.value)); }}>
            {frameBinding.frame_keys.map((value, index) => <option key={index} value={index}>{value}</option>)}
          </select>
        </Field>
        <Button variant="plain" size="sm" disabled={frame.index === 0} onClick={() => { setPinnedState(null); frame.seek(frame.index - 1); }}>{t("frame_previous")}</Button>
        <Button variant="plain" size="sm" disabled={frame.index === frameBinding.frame_keys.length - 1} onClick={() => { setPinnedState(null); frame.seek(frame.index + 1); }}>{t("frame_next")}</Button>
        <Button variant="plain" size="sm" disabled={!frame.playing && frame.index === frameBinding.frame_keys.length - 1} onClick={() => { setPinnedState(null); if (frame.playing) frame.pause(); else frame.play(); }}>{t(frame.playing ? "frame_pause" : "frame_play")}</Button>
        {frame.changed ? <Button variant="plain" size="sm" onClick={() => { setPinnedState(null); frame.reset(); }}>{t("frame_reset")}</Button> : null}
        <ChartDateEntry key={`${epoch}-frame`} domain={frameBinding.frame_keys} range={{ start: frame.index, end: frame.index }} single onApply={({ start }) => { setPinnedState(null); frame.seek(start); }} />
        <p className="basis-full text-xs text-muted-foreground">{frameScope?.label} · {t("frame_export_scope")}</p>
      </div> : null}
      {activeInWindow ? <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground" data-chart-focus-status>
        <span className="min-w-0 break-words">{t("focus_selected", { value: activeInWindow.selection.tuple.map((value) => value ?? "—").join(" · ") })}</span>
        <Button variant="plain" size="sm" className="shrink-0" onClick={() => { setLocal(null); setPinnedState(null); }}>{t("focus_clear")}</Button>
      </div> : null}
        {/* Keep live regions outside the editable document: modal inert attributes must not reparse prose or remount its figures. */}
      {focusEnabled ? <FloatingPortal><span className="sr-only" role="status">{[
        guideEnabled ? `${t("guide_step", { current: guideIndex + 1, total: guideSteps!.length })} · ${guideStep!.title}` : null,
        windowEnabled ? scope?.label ?? t("window_full") : null,
        activeInWindow ? t("focus_selected", { value: activeInWindow.selection.tuple.map((value) => value ?? "—").join(" · ") }) : null,
      ].filter(Boolean).join(". ")}</span></FloatingPortal> : null}
      <ChartPlanNotice spec={spec} />
      <ChartCalculations spec={spec} />
    </section>
  );
}

function ChartPlanNotice({ spec }: { spec: ChartSpec }) {
  const t = useTranslations("provenance");
  const plan = evidencePlanForViewer(spec.presentation_plan);
  if (spec.presentation_plan === undefined || (plan?.dataset.availability === "available" && plan.evidence_notes.every((note) => note.availability === "available"))) return null;
  return <p className="mt-3 text-xs text-muted-foreground" data-chart-plan-unavailable>{t(plan?.dataset.availability === "available" ? "note_unavailable" : "plan_unavailable")}</p>;
}

function ChartCalculations({ spec }: { spec: ChartSpec }) {
  const t = useTranslations("chart");
  if (!spec.derived_columns?.length) return null;
  const missing = Object.values(spec.calculation_missing ?? {}).reduce(
    (total, count) => total + count,
    0,
  );
  return (
    <div className="mt-3 text-xs text-muted-foreground" data-chart-calculations>
      {spec.derived_columns.map((column) => (
        <p key={column.key}>
          {column.header} ={" "}
          {column.expression.replace(
            /\b[a-zA-Z_][a-zA-Z_0-9]*\b/g,
            (name) => column.inputs[name] ?? name,
          )}
          {column.unit ? ` (${column.unit})` : ""}
        </p>
      ))}
      {missing > 0 ? <p>{t("calculation_missing", { count: missing })}</p> : null}
    </div>
  );
}

function SingleChartBlock({
  spec: rawSpec,
  sectionId,
  presentationRootId,
  presentationIdentity,
  detailReturnRef,
  onKindChange,
  surface,
  captionText,
  refLines,
  chrome: chromeProp,
  chromeless: chromelessProp,
  kindSwitch,
  actions: showEmbedActions,
  headerSlot,
  labelledBy,
  exactValues,
  exactValueLabels,
  renderPlacement: renderPlacementProp,
  renderMode,
  frozenPresentation,
  frozenRenderContext,
  hiddenSeries,
  onToggleSeries,
}: {
  spec: ChartSpec;
  sectionId?: string;
  presentationRootId?: string;
  presentationIdentity?: string;
  detailReturnRef?: React.RefObject<HTMLElement | null>;
  onKindChange?: (kind: ChartSpec["kind"]) => void;
  /** Batch 1 2.2: default numeric-label policy follows surface (export=always on). Omitted chat surface behaves like canvas. */
  surface?: BuildOpts["surface"];
  /** Batch 1 2.1b: attached caption text (from figure-ref) feeds protagonist parsing. */
  captionText?: string | null;
  /** Batch 2 §3.3: reference lines (from inspection, absent in report chart). */
  refLines?: RefLineSpec[];
  /** Frame by placement (D4); see `ChartChrome`. Default `report`. */
  chrome?: ChartChrome;
  /** Alias of `chrome="none"`, kept for existing call sites.
   *  Batch 2 §3.1: inspection chart has no own header — title at layer top, actions at bottom;
   *  another chart header triples duplication (caught in device acceptance). */
  chromeless?: boolean;
  /** `embed` only: show the chart-kind switch (rendered into `headerSlot`). */
  kindSwitch?: boolean;
  /** `embed` only: show inspect / ⋯ actions (rendered into `headerSlot`). */
  actions?: boolean;
  /** `embed` only: host DOM node (right side of the host's title row) that receives the opted-in controls. */
  headerSlot?: HTMLElement | null;
  /** `embed` only: id of the host's visible title, which names the figure. */
  labelledBy?: string;
  /** Exact-value disclosure below the chart (D5). Default `disclosure` for `embed`, `none` otherwise. */
  exactValues?: "disclosure" | "none";
  /** Full category names for the exact-value table, keyed by the category value the chart shows (short name →
   *  full name). Keyed, not positional: row completion or a kind switch may reorder or extend the rows. */
  exactValueLabels?: Readonly<Record<string, string>>;
  /** Settings/audit previews share the renderer without pretending to be report content. */
  renderPlacement?: ChartPlacement;
  /** Local preview mode; the real app continues to follow the document theme. */
  renderMode?: "light" | "dark";
  /** Trusted export-only values; ordinary screen surfaces resolve dynamically. */
  frozenPresentation?: ResolvedChartPresentationV2;
  frozenRenderContext?: ChartRenderContext;
  /** Controlled series visibility (from inspection). Omitted = own chart state (canvas legend entry).
   *  Both use **same** state and criterion from `series-visibility.ts`. */
  hiddenSeries?: readonly SeriesKey[];
  onToggleSeries?: (key: SeriesKey) => void;
}) {
  const tBlock = useTranslations("chart_block");
  const focus = useContext(ChartFocusContext);
  const tChart = useTranslations("chart");
  const chrome: ChartChrome = chromeProp ?? (chromelessProp ? "none" : "report");
  // Every existing "no own header" rule (inspection, exact tables) keys off this; `embed` is not chromeless —
  // it keeps truncation notes and row completion, it only gives the title to its host.
  const chromeless = chrome === "none";
  const renderPlacement = renderPlacementProp ?? (chrome === "embed" ? "embed" : undefined);
  const exactMode = exactValues ?? (chrome === "embed" ? "disclosure" : "none");
  const { registerPresentationOverride } = useCanvasContext();
  const [overrideKind, setOverrideKindState] = useState<ChartSpec["kind"] | null>(null);
  const restoreTransientOverride = useCallback(() => setOverrideKindState(null), []);
  useEffect(restoreTransientOverride, [presentationIdentity, restoreTransientOverride]);
  const overrideOwnerId = sectionId ?? presentationRootId;
  useEffect(() => {
    if (!overrideOwnerId || !overrideKind || onKindChange || !registerPresentationOverride) return;
    return registerPresentationOverride(overrideOwnerId, restoreTransientOverride);
  }, [onKindChange, overrideKind, registerPresentationOverride, restoreTransientOverride, overrideOwnerId]);
  const kind = overrideKind ?? rawSpec.kind;
  // Complete existing chart rows (see `complete-chart-rows.ts`) only on path actually drawing chart.
  // Tables have "展开全部", KPI one row, inspection completes itself (its fewer rows are user-selected Top-N) — leave unchanged.
  // No export completion: chart style frozen against stored spec (`normalizedRenderShape` includes series count);
  // new rows may add series absent at freeze; export honestly says "仅显示前 N 项".
  // Complete only **backend-classified charts**: never existing tables switched to bars by user, which would draw hundreds/thousands of bars
  // (backend bars first take top-N); honest "仅显示前 50 项" is preferable.
  const spec = useCompleteChartRows(
    rawSpec,
    !chromeless && surface !== "export" && isDrawnChartKind(rawSpec.kind) && isDrawnChartKind(kind),
  );
  const effectiveSpec = useMemo(
    () => (overrideKind ? { ...spec, kind: overrideKind } as ChartSpec : spec),
    [spec, overrideKind],
  );
  // review R4-7: fallback formerly inlined `{...effectiveSpec, kind: "table"}`, creating new object per
  // render; ChartTable spec-change reset effect then cleared expansion/sort on every parent rerender
  // and refetched export data. Memoize.
  const tableSpec = useMemo(
    () => ({ ...effectiveSpec, kind: "table" } as ChartSpec),
    [effectiveSpec],
  );
  // The export frame cannot grow: a long horizontal bar chart draws the rows that fit and discloses the cut
  // (see `fitHbarRowsForExport`). Screens keep every row — they grow and scroll instead.
  const exportFrameHeight = frozenRenderContext?.bounds.heightPx ?? defaultChartRenderBounds("export").heightPx;
  const drawSpec = useMemo(
    () => (surface === "export" ? fitHbarRowsForExport(effectiveSpec, exportFrameHeight, hiddenSeries) : effectiveSpec),
    [effectiveSpec, exportFrameHeight, hiddenSeries, surface],
  );
  const unitGaps = useMemo(() => drawSpec._export_analytical ? [] : checkKindUnits(drawSpec, kind), [drawSpec, kind]);
  const compatibles = useMemo(() => spec._export_analytical ? [spec.kind] : _compatibleKinds(spec).filter((kind) => !focus || observationViewEligible({ ...(focus.scopeSource ?? spec), kind }, focus.binding)), [spec, focus]);
  // Batch 3 4.3: user-readable tooltip. Previously printed raw inferer `_infer_reason`
  // (e.g. "1 cat + 1 num, n=6 → bar"), same issue as 0-C internal field paths
  // entering user-facing text. Keep raw only in dev to debug infer selection.
  const switcherTitle = overrideKind
    ? tBlock("switcher.manual")
    : tBlock("switcher.auto")
      + (process.env.NODE_ENV !== "production" && spec._infer_reason ? ` · ${spec._infer_reason}` : "");
  const switcherAllowed = chrome === "report" || (chrome === "embed" && kindSwitch === true);
  const switcher = switcherAllowed && (compatibles.length > 1 || spec._export_analytical) ? (
    <KindSwitcher
      current={kind}
      kinds={compatibles}
      onPick={(k) => {
        focus?.closeDetails?.();
        // Batch 0 0-F (chart-table-ux tech-design §1.6): chart-kind switching previously had zero telemetry —
        // among four persistent buttons, only "打开数据" usage known. from/to directly informs
        // infer.py selection quality (input to inspection-batch decisions).
        track("bowhead_chart_action_total", { action: "kind_switch" });
        track("bowhead_chart_kind_switch_total", { from: spec.kind, to: k });
        if (onKindChange) {
          setOverrideKindState(null);
          onKindChange(k);
          return;
        }
        setOverrideKindState(k === spec.kind ? null : k);
      }}
      title={switcherTitle}
    />
  ) : null;
  // D4 for the non-Chart.js forms (KPI, table, fallbacks): only `report` draws its own header and frame. In `embed` the
  // host's panel is the frame, and a kind switch the host enabled moves to the host slot so it stays usable.
  const frameless = chrome !== "report";
  const ownSwitcher = frameless ? undefined : switcher;
  const embedSwitch = chrome === "embed" && switcher && surface !== "export"
    ? <EmbedControlsSlot headerSlot={headerSlot}><div className="v2-chart-actions">{switcher}</div></EmbedControlsSlot>
    : null;

  if (effectiveSpec._export_analytical?.facet) {
    const profile = effectiveSpec._export_analytical;
    return <ChartExportSurface sectionId={sectionId} effectiveKind={kind}>
      <div className="@container min-w-0" data-chart-facets>
        {!chromeless ? <div className="v2-chart-header"><div className="v2-chart-title">{spec.title}</div>{spec.subtitle ? <div className="v2-chart-subtitle">{spec.subtitle}</div> : null}</div> : null}
        <div className="grid grid-cols-1 gap-6 @min-[760px]:grid-cols-2 @min-[1120px]:grid-cols-3">
          {profile.binding.group_values!.map((group) => {
            const rows = effectiveSpec.rows!.filter((row) => tupleKey([row[profile.facet!.key]]) === tupleKey([group]));
            const { facet: _facet, ...paneProfile } = profile;
            const pane = { ...effectiveSpec, rows, row_count: rows.length, title: String(group), subtitle: undefined, _export_analytical: paneProfile };
            return <div className="min-w-0" key={tupleKey([group])} data-chart-facet={String(group)}><SingleChartBlock spec={pane} surface={surface} chrome={chrome} presentationRootId={overrideOwnerId} presentationIdentity={presentationIdentity} detailReturnRef={detailReturnRef} renderMode={renderMode} /></div>;
          })}
        </div>
      </div>
    </ChartExportSurface>;
  }

  // KPI / table bypass Chart.js — no outsourced wrapper; ChartTable/ChartKPI own headers
  if (kind === "kpi") {
    // Batch 1 2.4 (1-F): formerly own .v2-chart-header (title) + .v2-chart-block (card frame)
    // overlapped ChartKPI .v2-chart-kpi-title / frame into **double titles/frames** (production
    // prod-kpi-1440.png recording). ChartKPI alone owns title/actions.
    return (
      <ChartExportSurface sectionId={sectionId} effectiveKind={kind}>
        {embedSwitch}
        <ChartKPI spec={effectiveSpec} switcher={ownSwitcher} embedded={frameless} />
        <ChartSubcaptions spec={spec} annotationSpec={effectiveSpec} chromeless={chromeless} detailReturnRef={detailReturnRef} surface={surface} />
      </ChartExportSurface>
    );
  }
  if (kind === "table") {
    return (
      <ChartExportSurface sectionId={sectionId} effectiveKind={kind}>
        {embedSwitch}
        <ChartTable
          spec={effectiveSpec}
          presentationRootId={overrideOwnerId}
          switcher={ownSwitcher}
          surface={surface}
          sectionId={sectionId}
          chromeless={frameless}
        />
        <ChartSubcaptions spec={spec} annotationSpec={effectiveSpec} chromeless={chromeless} detailReturnRef={detailReturnRef} surface={surface} />
      </ChartExportSurface>
    );
  }

  if (unitGaps.length) {
    return (
      <ChartExportSurface sectionId={sectionId} effectiveKind="table">
        {embedSwitch}
        <ChartTable spec={tableSpec} switcher={ownSwitcher} surface={surface} sectionId={sectionId} chromeless={frameless} />
        <div data-testid="chart-incompatible-units" style={SUBCAPTION_STYLE}>
          <CharIcon ch="ⓘ" /> {tChart("incompatible_axis_units")}
        </div>
        <ChartSubcaptions spec={spec} annotationSpec={effectiveSpec} chromeless={chromeless} detailReturnRef={detailReturnRef} surface={surface} />
      </ChartExportSurface>
    );
  }

  // Readability audit item 1: rows that are not a flow (independent bands, or a node emitting far more than it
  // receives — mixed units) would be drawn as widths that compare visits with yuan. Show the exact table with the
  // reason instead. A plain table, not `ChartFallbackTable`: the export page treats the fallback frame as a render
  // failure, and these persisted charts exported before; new charts never get here (`infer.py` emits a table).
  if (kind === "sankey" && notAFlowReason(effectiveSpec)) {
    return (
      <ChartExportSurface sectionId={sectionId} effectiveKind="table">
        {embedSwitch}
        <ChartTable
          spec={tableSpec}
          switcher={ownSwitcher}
          surface={surface}
          sectionId={sectionId}
          chromeless={frameless}
        />
        <div data-testid="chart-not-a-flow" style={SUBCAPTION_STYLE}>
          <CharIcon ch="ⓘ" /> {tChart("not_a_flow")}
        </div>
        <ChartSubcaptions spec={spec} annotationSpec={effectiveSpec} chromeless={chromeless} detailReturnRef={detailReturnRef} surface={surface} />
      </ChartExportSurface>
    );
  }

  const SUPPORTED_CHARTJS: ChartSpec["kind"][] = [
    "line", "bar", "hbar", "pie", "stacked_bar", "dual_axis", "combo",
    "area_stacked", "scatter",
    "waterfall", "sankey", "heatmap",  // WP2
  ];
  if (!SUPPORTED_CHARTJS.includes(kind)) {
    return (
      <ChartExportSurface sectionId={sectionId} effectiveKind={kind}>
        {embedSwitch}
        <ChartFallbackTable spec={tableSpec} sourceKind={kind} reason="unsupported_kind" switcher={ownSwitcher} surface={surface} sectionId={sectionId} chromeless={frameless} />
        <ChartSubcaptions spec={spec} annotationSpec={effectiveSpec} chromeless={chromeless} detailReturnRef={detailReturnRef} surface={surface} />
      </ChartExportSurface>
    );
  }

  return (
    <ChartExportSurface sectionId={sectionId} effectiveKind={kind}>
      {/* Batch 3 4.3 (P2-3): fallback table also gets switcher — rendering failure is when users most need to try another kind;
          previously switcher vanished on this path, leaving users staring helplessly at table. */}
      <ErrorBoundary fallback={<>{embedSwitch}<ChartFallbackTable spec={tableSpec} sourceKind={kind} reason="render_error" switcher={ownSwitcher} surface={surface} sectionId={sectionId} chromeless={frameless} /></>}>
        <ChartJsBlock
          spec={drawSpec}
          switcher={chromeless ? undefined : switcher}
          surface={surface}
          sectionId={sectionId}
          captionText={captionText}
          refLines={refLines}
          chrome={chrome}
          showEmbedActions={showEmbedActions === true}
          headerSlot={headerSlot}
          labelledBy={labelledBy}
          hasExactValues={exactMode === "disclosure"}
          exactValueLabels={exactValueLabels}
          renderPlacement={renderPlacement}
          renderMode={renderMode}
          frozenPresentation={frozenPresentation}
          frozenRenderContext={frozenRenderContext}
          hiddenSeries={hiddenSeries}
          onToggleSeries={onToggleSeries}
        />
        <ChartSubcaptions spec={drawSpec} chromeless={chromeless} drawn detailReturnRef={detailReturnRef} surface={surface} />
      </ErrorBoundary>
    </ChartExportSurface>
  );
}

/** Embedded-chart controls go to the host's title-row slot; without one, a bare right-aligned row (never a band). */
function EmbedControlsSlot({ headerSlot, children }: { headerSlot?: HTMLElement | null; children: React.ReactNode }) {
  return headerSlot ? createPortal(children, headerSlot) : <div className="v2-chart-embed-actions">{children}</div>;
}

/**
 * 「查看精确数值」 (Chart Embed Spec D5): one disclosure for every host instead of each host writing its own.
 * Native details/summary (screen readers announce the collapsed state), a line chevron instead of the browser
 * triangle, and the table mounted only once opened. The table is the borderless `chrome="none"` table with full
 * category names (`labels`, one per row) when the chart shows shortened ones.
 */
function ChartExactValues({ spec, labels }: { spec: ChartSpec; labels?: Readonly<Record<string, string>> }) {
  const t = useTranslations("chart_block");
  const [open, setOpen] = useState(false);
  const tableSpec = useMemo<ChartSpec>(() => {
    const labelCol = spec.columns?.find((c) => c.type !== "numeric");
    const rows = labels && labelCol
      ? (spec.rows ?? []).map((row) => {
          const full = labels[String(row[labelCol.key] ?? "")];
          return full ? { ...row, [labelCol.key]: full } : row;
        })
      : spec.rows;
    return { ...spec, kind: "table", rows } as ChartSpec;
  }, [labels, spec]);
  return (
    <details className="v2-chart-exact" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="v2-chart-exact-summary">
        <LucideGlyph icon={ChevronRight} size={14} aria-hidden="true" className="v2-chart-exact-chevron" />
        {t("exact_values")}
      </summary>
      {open ? (
        <div className="v2-chart-exact-body">
          <ChartBlock spec={tableSpec} chrome="none" surface="canvas" />
        </div>
      ) : null}
    </details>
  );
}

function ChartExportSurface({
  sectionId,
  effectiveKind,
  children,
}: {
  sectionId?: string;
  effectiveKind: ChartSpec["kind"];
  children: React.ReactNode;
}) {
  const sharedScale = useContext(ChartSharedScaleContext);
  const t = useTranslations("chart_block");
  const notice = sharedScale && sharedScale.encoding !== effectiveKind ? <p className="mt-2 text-xs text-muted-foreground" data-chart-shared-scale-paused>{t("shared_scale_paused")}</p> : null;
  if (!sectionId) return <>{children}{notice}</>;
  return (
    <div
      data-export-section-id={sectionId}
      data-export-effective-kind={effectiveKind}
      style={{ display: "contents" }}
    >
      {children}
      {notice}
    </div>
  );
}

/** Batch 1 2.3 (P2-7): heatmap color-scale legend — gradient + precise endpoints; otherwise shade meanings are guesses. */
function HeatmapLegendStrip({ legend }: { legend: NonNullable<ChartJSConfig["heatmapLegend"]> }) {
  const theme = useChartTheme();
  const format = (value: number) => legend.percent ? formatObservedPercentCell(value, legend.percent)
    : legend.display ? formatDisplayValue(value, legend.display) : formatPrecise(value, legend.unit);
  return (
    <div
      className="v2-chart-heatmap-legend"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        marginTop: 8,
        fontSize: "0.6875rem",
        fontVariantNumeric: "tabular-nums",
        color: theme.inkSoft,
      }}
    >
      <span>{format(legend.min)}</span>
      <span
        aria-hidden
        style={{
          flex: "0 1 160px",
          height: 8,
          borderRadius: 4,
          background: `linear-gradient(to right, ${legend.from}, ${legend.to})`,
        }}
      />
      <span>{format(legend.max)}</span>
    </div>
  );
}

const SUBCAPTION_STYLE: CSSProperties = {
  marginTop: 6,
  fontSize: "0.75rem",
  color: "var(--c-ink-3)",
  fontStyle: "italic",
};

/** Chart subcaption. **Accept entire spec, not individual fields** — each of four render branches calls once;
 *  field-based arguments would require four changes per new notice, and missed branch means omitted explanation.
 *
 *  Two existing notices:
 *   - `auto_charted` (Sprint ι PRD §4.C.2): chart automatically added by backend 5-AND gate.
 *   - `top_n` (chart-config-ux batch 3 ⑤): backend took top-N above row ceiling. **Cannot omit**:
 *     table footer has "显示 1-30 / 共 98", but Chart.js path lacked equivalent — silently
 *     omitting 68 rows misrepresents data. `row_count` still genuine total; both numbers needed for notice. */
function ChartSubcaptions({
  spec,
  chromeless,
  drawn,
  annotationSpec = spec,
  detailReturnRef,
  surface,
}: {
  spec: ChartSpec;
  annotationSpec?: ChartSpec;
  detailReturnRef?: React.RefObject<HTMLElement | null>;
  surface?: BuildOpts["surface"];
  chromeless?: boolean;
  /** Chart.js path draws available rows; report available count. */
  drawn?: boolean;
}) {
  const t = useTranslations();
  const focus = useContext(ChartFocusContext);
  const target = surface !== "export" && !chromeless ? focus?.pinned : null;
  const pinnedAnnotation = target?.annotationId ? annotationSpec._export_annotations?.find((item) => item.id === target.annotationId && annotationCaption(item, annotationSpec) !== null) : null;
  const readableTarget = target && (!target.annotationId || pinnedAnnotation) && focus && observationViewEligible(annotationSpec, focus.binding) && observationRows(target, annotationSpec, focus.binding)?.length ? target : null;
  useEffect(() => { if (target && !readableTarget) focus?.closeDetails?.(); }, [target, readableTarget, focus]);
  const total = spec.row_count ?? 0;
  const have = (spec.rows ?? []).length;
  // Chart (`drawn`): **draws `spec.rows`**; disclose fewer rows than total regardless of `top_n`.
  //   Previously only `top_n`, justified by claim inline row truncation does not change plotted item count —
  //   incorrect: a 72-row monthly chart may inline only 50 rows,
  //   omit top_n, and silently stop before the final month. Existing charts now complete rows first
  //   (`useCompleteChartRows`); success naturally hides notice; failed / ongoing completion / export disclose truthfully.
  //   Available count instead of `top_n` also corrects old Sankey label 60 while drawing 50.
  // Other branches (table own footer, KPI, fallback table) keep old criterion: disclose only when backend sets `top_n`.
  const shown = drawn ? (have < total ? have : 0) : (spec.top_n ?? 0);
  //
  // ⚠ **Never show in** inspection (chromeless) — 116 device found `ensureFullRows`
  // already fetched all 40 rows, footer "显示 40 / 共 40 行", while this still said
  // "仅显示前 30 项", falsely (original spec `top_n` persists while layer renders completed rows).
  // User-selected Top-5 in layer makes it worse. Yield to layer's own more accurate scope.
  //
  // Series truncation (backend `_cap_time_series`) needs **different counting unit**: 59 provinces capped to 15 lines
  // changes rows 708→180; row-based "仅显示前 180 项" misrepresents series count as rows.
  // Missing 528 rows are **exactly** removed 44 provinces; both notices describe same operation,
  // so show only correct one, without stacking.
  const seriesShown = spec.series_shown ?? 0;
  const seriesTotal = spec.series_total ?? 0;
  const seriesCut = !chromeless && seriesShown > 0 && seriesTotal > seriesShown;
  // Downsampling is **third** scope: neither fewer rows nor fewer lines, but
  // sparser time points spanning same full period. All three may coexist; explain each separately, never merge notices.
  const ptsShown = spec.points_shown ?? 0;
  const ptsTotal = spec.points_total ?? 0;
  const pointsCut = !chromeless && ptsShown > 0 && ptsTotal > ptsShown;
  // ⚠ Yield only when **all** row reduction comes from series truncation — backend then leaves `top_n` unset.
  // Set `top_n` indicates separate cut (upstream `limit`, size gate…); that cut still requires disclosure,
  // otherwise only series notice remains, hiding 708→400 row loss entirely (observed in rereview).
  // Downsampling also reduces available rows, but does not mean only first N items plotted — such wording
  // misstates sparse points across the full period as an unplotted tail.
  const truncated =
    !chromeless && !pointsCut && !(seriesCut && spec.top_n == null) && shown > 0 && total > shown;
  const annotations = (annotationSpec._export_annotations ?? []).map((item) => ({ item, caption: annotationCaption(item, annotationSpec) })).filter((entry) => entry.caption !== null);
  if ((chromeless || (!spec.auto_charted && !truncated && !seriesCut && !pointsCut)) && !annotations.length && !readableTarget && !spec._export_analytical?.summary_keys?.length) return null;
  return (
    <>
      {pointsCut ? (
        <div data-testid="chart-downsampled-subcaption" style={SUBCAPTION_STYLE}>
          <CharIcon ch="ⓘ" /> {t("chart.downsampled_points", { shown: ptsShown, total: ptsTotal })}
        </div>
      ) : null}
      {seriesCut ? (
        <div data-testid="chart-series-subcaption" style={SUBCAPTION_STYLE}>
          <CharIcon ch="ⓘ" /> {t("chart.truncated_series", { shown: seriesShown, total: seriesTotal })}
        </div>
      ) : null}
      {truncated ? (
        <div data-testid="chart-truncated-subcaption" style={SUBCAPTION_STYLE}>
          <CharIcon ch="ⓘ" />{" "}
          {spec._export_top
            ? t("chart.truncated_top_by", spec._export_top)
            : t("chart.truncated_top_n", { shown, total })}
        </div>
      ) : null}
      {spec.auto_charted && !chromeless ? (
        <div data-testid="auto-chart-subcaption" style={SUBCAPTION_STYLE}>
          <CharIcon ch="ⓘ" /> {t("chart.auto_from_sandbox")}
        </div>
      ) : null}
      {annotations.map(({ item, caption }, index) => <div key={item.id} data-chart-annotation={item.id} style={SUBCAPTION_STYLE}>
        {surface !== "export" && !chromeless && focus?.inspectAnnotation ? <Button variant="plain" size="sm" className="chart-annotation-action" style={{ maxWidth: "100%", height: "auto", textAlign: "left", whiteSpace: "normal", justifyContent: "flex-start" }} aria-pressed={target?.annotationId === item.id}
          onClick={(event) => { event.stopPropagation(); event.currentTarget.focus({ preventScroll: true }); focus.inspectAnnotation!(item); }}>{index + 1}. {item.role === "coverage" ? t("chart_block.annotation_coverage") + " · " : item.role === "event" ? t("chart_block.annotation_event") + " · " : ""}{caption}<span className="chart-annotation-details">{t("chart_block.annotation_details")}</span></Button> : <>{item.role === "coverage" ? t("chart_block.annotation_coverage") + " · " : item.role === "event" ? t("chart_block.annotation_event") + " · " : ""}{caption}</>}
      </div>)}
      {spec._export_analytical?.summary_keys?.length ? <AnalyticalSummaryCaptions spec={spec} /> : null}
      {readableTarget && focus ? <ChartObservationDetails spec={annotationSpec} target={readableTarget} binding={focus.binding}
        annotation={pinnedAnnotation ?? undefined} onClose={focus.closeDetails!} returnFocusRef={detailReturnRef} /> : null}
    </>
  );
}

/** Explain `ShapeGap` in user terms: data cannot draw X, still needs Y.
 *
 *  Say only **what is missing**, not what is present: user already sees table and can count columns;
 *  unknown is what a pie actually needs. */
function shapeMismatchLabel(
  t: ReturnType<typeof useTranslations>,
  kind: ChartSpec["kind"],
  gap?: ShapeGap | null,
): string {
  const kindLabel = (() => {
    try {
      return t(`kind.${kind}`);
    } catch {
      return kind;
    }
  })();
  if (!gap) return t("fallback.shape_mismatch_generic", { kind: kindLabel });
  if (gap.signedShare) return t("fallback.signed_share");
  if (gap.unsafeIntegerKeys?.length) return t("fallback.unsafe_integer");
  const missing: string[] = [];
  if (gap.needDims) missing.push(t("fallback.need_dims", { n: gap.needDims }));
  if (gap.needNumeric) missing.push(t("fallback.need_numeric", { n: gap.needNumeric }));
  return t("fallback.shape_mismatch", { kind: kindLabel, missing: missing.join(t("fallback.and")) });
}

function ChartFallbackTable({
  spec,
  sourceKind,
  reason,
  gap,
  switcher,
  surface,
  sectionId,
  chromeless,
}: {
  spec: ChartSpec;
  sourceKind: ChartSpec["kind"];
  reason: "unsupported_kind" | "no_canvas" | "config_error" | "render_error" | "shape_mismatch";
  /** `shape_mismatch` gap, explaining missing requirements to user. */
  gap?: ShapeGap | null;
  switcher?: React.ReactNode;
  /** Omit table's own header inside inspection (R4-8). */
  chromeless?: boolean;
  // review P3: fallback table formerly omitted surface, collapsing to 10 rows on export.
  // Export fallback detector currently catches it (whole job uses legacy), but unrelated gate.
  surface?: BuildOpts["surface"];
  sectionId?: string;
}) {
  const t = useTranslations("chart_block");
  useEffect(() => {
    track("bowhead_chart_fallback_table_total", {
      reason,
      kind: sourceKind,
    });
  }, [reason, sourceKind]);

  const reasonLabel = (() => {
    if (reason === "unsupported_kind") return t("fallback.unsupported_kind");
    if (reason === "no_canvas") return t("fallback.no_canvas");
    if (reason === "config_error") return t("fallback.config_error");
    if (reason === "shape_mismatch") return shapeMismatchLabel(t, sourceKind, gap);
    return t("fallback.render_error");
  })();

  return (
    <div className="v2-chart-fallback-wrap">
      <div className="v2-chart-fallback-note">
        <strong>{t("fallback.title")}</strong>
        <span>{reasonLabel}</span>
      </div>
      {/* ⑨ (bug 0708): the chart spec is column-pruned + number-formatted for the
          CHART (e.g. 省+退款额 only, "5亿"). A table fallback ≠ a chart, so render
          the ORIGINAL artifact (all columns, raw precision) when we have its id;
          fall back to the pruned chart-derived table only when there's no artifact
          or its fetch fails (shared-report 403), so we're never worse than before. */}
      {/* review R6-3: chromeless (inspection) cannot use original artifact path. ArtifactPreview
          has own header/actions, ignores chromeless and renders **original stored table**, discarding user-selected
          Top-N / hidden series while layer footer still counts derived view.
          Render derived-spec table directly in layer. */}
      {spec.data_artifact_id && chartDatasetAvailable(spec) && !chromeless ? (
        <ChartFallbackArtifact
          fileId={spec.data_artifact_id}
          title={spec.title}
          fallback={<ChartTable spec={spec} switcher={switcher} surface={surface} sectionId={sectionId} chromeless={chromeless} />}
        />
      ) : (
        <ChartTable spec={spec} switcher={switcher} surface={surface} sectionId={sectionId} chromeless={chromeless} />
      )}
    </div>
  );
}

/** ⑨: fetch + render the original CSV/artifact behind a chart whose config failed
 *  to build — full columns + raw values via the same preview endpoint the Data tab
 *  uses. On error, degrade to the caller's `fallback` (the pruned chart table). */
function ChartFallbackArtifact({
  fileId,
  title,
  fallback,
}: {
  fileId: string;
  title?: string;
  fallback: React.ReactNode;
}) {
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(false);
    fetchFilePreview(fileId, { mode: "auto" })
      .then((p) => { if (alive) { setPreview(p); setLoading(false); } })
      .catch(() => { if (alive) { setError(true); setLoading(false); } });
    return () => { alive = false; };
  }, [fileId]);

  if (error) return <>{fallback}</>;
  // No mime; filename is chart title (no .csv suffix) ⇒ viewer cannot infer type from name, marks
  // unsupported and shows "暂无可渲染预览" despite returned rows. Set table type from actual preview shape.
  const rows = preview?.mode === "rows" && preview.columns && preview.rows;
  return (
    <ArtifactPreview
      fileId={fileId}
      filename={title || fileId}
      profile={rows ? "table" : undefined}
      preview={preview}
      loading={loading}
      showHeaderTitle={false}
    />
  );
}

/** Chart-kind dropdown (selection menu). Mount on body, position with `useMenu`: table form avoids
 *  `.v2-chart-table` overflow cropping; report avoids action-bar fade opacity;
 *  flip upward near viewport bottom (design "图层" section item 5). */
function KindSwitcher({
  current, kinds, onPick, title,
}: {
  current: ChartSpec["kind"];
  kinds: ChartSpec["kind"][];
  onPick: (k: ChartSpec["kind"]) => void;
  title?: string;
}) {
  const t = useTranslations("chart_block");
  const labelOf = (k: ChartSpec["kind"]): string => {
    try {
      return t(`kind.${k}`);
    } catch {
      return k;
    }
  };
  const [open, setOpen] = useState(false);
  const reportOpen = useContext(ChartActionsLeadOpenContext);
  const menuRoot = useContext(ChartMenuRootContext);
  useEffect(() => {
    if (!reportOpen) return;
    reportOpen(open);
    return () => reportOpen(false);
  }, [open, reportOpen]);
  const selected = kinds.indexOf(current);
  const menu = useMenu({
    open,
    onOpenChange: setOpen,
    placement: "bottom-end",
    offset: 4,
    selectedIndex: selected < 0 ? null : selected,
  });
  return (
    <>
      <button
        type="button"
        ref={menu.refs.setReference}
        className="v2-chart-action-btn"
        title={title}
        {...menu.getReferenceProps()}
      >
        {labelOf(current)}
        <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" className="v2-chart-action-chevron" />
      </button>
      <MenuFloating menu={menu} root={menuRoot}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({ className: "v2-chart-kind-menu" })}
        >
          {kinds.map((k) => (
            <KindMenuItem key={k} kind={k} label={labelOf(k)} checked={k === current} onPick={onPick} />
          ))}
        </div>
      </MenuFloating>
    </>
  );
}

function KindMenuItem({
  kind, label, checked, onPick,
}: {
  kind: ChartSpec["kind"];
  label: string;
  checked: boolean;
  onPick: (k: ChartSpec["kind"]) => void;
}) {
  const itemProps = useMenuItem({ role: "menuitemradio", checked, onSelect: () => onPick(kind) });
  return (
    <button type="button" className="v2-chart-kind-item" data-kind={kind} {...itemProps}>
      {label}
    </button>
  );
}

// #85 chart-kind switcher — recommend switchable kinds by data shape.
// UX-7 (2026-04-29): add combo / dual_axis / heatmap for multidimension/multimeasure cases,
// letting users manually choose another perspective after inferer default.
// Batch 3 4.3 (P1-6): three WP2 kinds formerly absent from allowlist — just pushed into compat, then filtered out,
// making waterfall/sankey/heatmap **impossible to return to after switching** (own kind absent from switcher).
const _SUPPORTED_KINDS = new Set<ChartSpec["kind"]>([
  "line", "bar", "hbar", "pie", "stacked_bar", "dual_axis", "combo",
  "area_stacked", "scatter", "waterfall", "sankey", "heatmap", "kpi", "table",
]);

/** **Sole** criterion for which chart kinds this spec shape supports.
 *
 *  Only difference from `_compatibleKinds`: **do not** forcibly prepend spec's current kind.
 *  That unshift serves switcher (must return after switching); using it for buildability
 *  always yields true. Detection differs from display; provide separate entries, never make callers subtract that item
 *  — duplicated criteria are recurring failures this sprint. */
function _shapeKinds(spec: ChartSpec): ChartSpec["kind"][] {
  const cols = spec.columns ?? [];
  const nCat = cols.filter((c: ChartColumn) => c.type === "categorical").length;
  const nDate = cols.filter((c: ChartColumn) => c.type === "date").length;
  const nNum = cols.filter((c: ChartColumn) => c.type === "numeric").length;
  const nRows = (spec.rows ?? []).length;
  const compat: ChartSpec["kind"][] = [];

  // Time-series group
  if (nDate >= 1 && nNum >= 1) {
    compat.push("line", "bar");
    if (nNum >= 2) compat.push("area_stacked", "dual_axis", "combo");
  }
  // 1 cat + ≥2 num: combo / dual_axis / stacked_bar all available
  if (nCat === 1 && nNum >= 2) {
    compat.push("combo", "dual_axis", "stacked_bar", "bar");
  }
  // Grouped category group (1 cat + 1 num)
  if (nCat === 1 && nNum === 1) {
    compat.push("bar", "hbar");
    if (nRows <= 15) compat.push("pie");
    if (nRows >= 2 && nRows <= 20) compat.push("waterfall");  // WP2: contribution-decomposition perspective
  }
  // Multidimension stacks (2+ cat + 1+ num)
  if (nCat >= 2 && nNum >= 1) {
    compat.push("stacked_bar", "bar");
    // WP2: flow perspective (cat1→cat2) — only when the rows actually form a flow (see `flow-shape.ts`).
    if (nRows <= 60 && !notAFlowReason(spec)) compat.push("sankey");
    // UX-7: high-cardinality cat → heatmap also reasonable (cell color maps num value)
    if (nRows >= 20) compat.push("heatmap");
    // UX-7: multiple num also allows combo (user may manually pivot cat1=x cat2=series)
    if (nNum >= 2) compat.push("combo");
  }
  // Correlation
  if (nNum >= 2) {
    compat.push("scatter");
  }
  // Single row, multiple columns → kpi
  if (nRows === 1 && nNum >= 1) {
    compat.push("kpi");
  }
  // All data can fall back to table
  compat.push("table");

  const seen = new Set<string>();
  const kinds = compat.filter((k) => {
    if (!_SUPPORTED_KINDS.has(k)) return false;
    if (checkKindUnits(spec, k).length) return false;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  return kinds;
}

function _compatibleKinds(spec: ChartSpec): ChartSpec["kind"][] {
  const kinds = _shapeKinds(spec);
  // Batch 3 4.3 (P1-6) fallback: current kind always in switcher, otherwise cannot return after switching.
  if (!kinds.includes(spec.kind) && _SUPPORTED_KINDS.has(spec.kind)) kinds.unshift(spec.kind);
  return kinds;
}

// ⑦: removed `canRenderKind`. Name claimed buildability, implementation was
// `_shapeKinds(spec).includes(kind)` — **recommendation set**, deliberately narrower than builder.
// Sole caller (inspection series-toggle downgrade) asked buildability, so hiding a line
// silently changed `1 date + 2 num` area_stacked to line (recommendation requires ≥2 numeric columns, buildLine
// needs only one). Caller now uses `lib/chart/kind-shape.ts` `canBuildKind`;
// retaining misleadingly named entry would invite next incorrect use.

/** Best shape-compatible kind; default table if none recommended (`_shapeKinds` always includes table). */
export function preferredKind(spec: ChartSpec): ChartSpec["kind"] {
  return _shapeKinds(spec)[0] ?? "table";
}

export function canRenderCanvasCharts() {
  return (
    typeof HTMLCanvasElement !== "undefined"
    && typeof HTMLCanvasElement.prototype.getContext === "function"
  );
}

function ChartJsBlock({
  spec, switcher, surface, sectionId, captionText, refLines, chrome,
  showEmbedActions, headerSlot, labelledBy, hasExactValues, exactValueLabels,
  renderPlacement, renderMode, frozenPresentation, frozenRenderContext,
  hiddenSeries: controlledHiddenSeries, onToggleSeries,
}: {
  spec: ChartSpec;
  switcher?: React.ReactNode;
  surface?: BuildOpts["surface"];
  sectionId?: string;
  captionText?: string | null;
  refLines?: RefLineSpec[];
  chrome: ChartChrome;
  showEmbedActions: boolean;
  headerSlot?: HTMLElement | null;
  labelledBy?: string;
  hasExactValues: boolean;
  exactValueLabels?: Readonly<Record<string, string>>;
  renderPlacement?: ChartPlacement;
  renderMode?: "light" | "dark";
  frozenPresentation?: ResolvedChartPresentationV2;
  frozenRenderContext?: ChartRenderContext;
  hiddenSeries?: readonly SeriesKey[];
  onToggleSeries?: (key: SeriesKey) => void;
}) {
  const t = useTranslations("chart_block");
  const focus = useContext(ChartFocusContext);
  const windowScope = useContext(ChartTimeWindowContext);
  const axisDomains = useContext(ChartSharedScaleContext);
  const tInspect = useTranslations("chart_inspect");
  const theme = useChartTheme();
  const chromeless = chrome === "none";
  const chartRef = useRef<ChartJS | null>(null);
  const plotRef = useRef<HTMLDivElement | null>(null);
  const [inspecting, setInspecting] = useState(false);
  // Inspection (DialogShell) close callback with stable reference.
  const closeInspect = useCallback(() => setInspecting(false), []);
  // Return focus to triggering button after closing. TableActions renders entry, so no ref available;
  // capture focused element at click time — that button on both keyboard and mouse activation.
  const inspectTriggerRef = useRef<HTMLElement | null>(null);
  const canvasSupported = canRenderCanvasCharts();

  // Localize Chinese x-axis time buckets (`2026年1月`) to English by locale. Pure function never reads window,
  // so locale enters opts like theme; omitted leaves unchanged (Chinese turns byte-identical).
  const runtimeLocale = useLocale();
  const locale = frozenRenderContext?.locale ?? runtimeLocale;
  const initialBounds = frozenRenderContext
    ? {
        widthPx: frozenRenderContext.bounds.widthPx,
        heightPx: frozenRenderContext.bounds.heightPx,
      }
    : defaultChartRenderBounds(surface ?? "canvas");
  const [plotBounds, setPlotBounds] = useState(initialBounds);
  const [inputCapabilities, setInputCapabilities] = useState<ChartRenderContext["input"]>({
    hover: true,
    pointer: "fine",
    keyboard: true,
  });
  const [reducedMotion, setReducedMotion] = useState(false);
  const [forcedColors, setForcedColors] = useState(false);

  useEffect(() => {
    if (frozenRenderContext) return;
    const element = plotRef.current;
    if (!element) return;
    const sync = () => {
      const rect = element.getBoundingClientRect();
      const widthPx = Math.round(rect.width);
      const heightPx = Math.round(rect.height);
      if (widthPx <= 0 || heightPx <= 0) return;
      setPlotBounds((current) => current.widthPx === widthPx && current.heightPx === heightPx
        ? current
        : { widthPx, heightPx });
    };
    sync();
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(sync);
      observer.observe(element);
      return () => observer.disconnect();
    }
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, [frozenRenderContext]);

  useEffect(() => {
    if (frozenRenderContext) return;
    if (typeof window.matchMedia !== "function") return;
    const safeQuery = (query: string) => {
      try {
        return window.matchMedia(query);
      } catch {
        return null;
      }
    };
    const hover = safeQuery("(hover: hover)");
    const fine = safeQuery("(pointer: fine)");
    const coarse = safeQuery("(pointer: coarse)");
    const motion = safeQuery("(prefers-reduced-motion: reduce)");
    const colors = safeQuery("(forced-colors: active)");
    const queries = [hover, fine, coarse, motion, colors].filter(
      (query): query is MediaQueryList => query !== null,
    );
    const sync = () => {
      setInputCapabilities({
        hover: Boolean(hover?.matches),
        pointer: fine?.matches ? "fine" : coarse?.matches ? "coarse" : "none",
        keyboard: true,
      });
      setReducedMotion(Boolean(motion?.matches));
      setForcedColors(Boolean(colors?.matches));
    };
    sync();
    for (const query of queries) {
      if (typeof query.addEventListener === "function") query.addEventListener("change", sync);
      else query.addListener?.(sync);
    }
    return () => {
      for (const query of queries) {
        if (typeof query.removeEventListener === "function") query.removeEventListener("change", sync);
        else query.removeListener?.(sync);
      }
    };
  }, [frozenRenderContext]);

  const renderContext = useMemo<ChartRenderContext>(() => frozenRenderContext ?? ({
    placement: renderPlacement ?? (surface === "chat" ? "chat" : surface === "export" ? "embed" : "canvas"),
    bounds: {
      ...plotBounds,
      devicePixelRatio: typeof window === "undefined" ? 1 : (window.devicePixelRatio || 1),
    },
    input: inputCapabilities,
    output: {
      medium: surface === "export" ? "raster" : "screen",
      static: surface === "export",
    },
    mode: renderMode ?? (
      typeof document !== "undefined" && document.documentElement.dataset.theme === "dark"
        ? "dark"
        : "light"
    ),
    locale,
    reducedMotion,
    forcedColors,
    // An embedded chart without the exact-value disclosure offers no other way to read exact numbers (it has no
    // inspect entry), which pushes the label policy to "label everything". With the disclosure (D5) it has one.
    exactDataAlternative: {
      kind: surface === "export" || (renderPlacement === "embed" && !chromeless && !hasExactValues)
        ? "none"
        : "inspect-table",
    },
  }), [
    chromeless,
    hasExactValues,
    frozenRenderContext,
    forcedColors,
    inputCapabilities,
    locale,
    plotBounds,
    reducedMotion,
    renderMode,
    renderPlacement,
    surface,
  ]);
  const normalizedShape = useMemo(() => normalizeChartForRenderingV1(spec, { scopeSource: focus?.scopeSource }).shape, [spec, focus?.scopeSource]);
  const contextRecipe = useMemo(
    () => resolveChartRenderRecipe(renderContext, normalizedShape),
    [normalizedShape, renderContext],
  );
  // ⑦: ask whether this data supports selected kind before build.
  //
  // Both lead to fallback table, but **wording must differ**: shape mismatch means user-selected kind unsupported
  // by data (user can change it), while build exception means our broken code. Formerly both showed
  // "图表配置生成失败" — 116 inspection corpus **26.9% of selectable combinations** triggered it
  // (dual_axis/combo/scatter each 64–66%), misdescribing selected chart unsuitable for data
  // as system failure.
  // Series visibility — **single state** (`series-visibility.ts`); controlled inspection input takes precedence,
  // otherwise chart owns state: canvas legend formerly used Chart.js default handler, changing **instance**
  // `meta.hidden`; `chartKey` includes theme/labels/all dataset values, so stream updates/theme switches
  // remounted instance and restored user-hidden series. React state reapplies hidden even after remount.
  // ★ Must precede both early returns below (hook-order guard in chart-block.test.tsx).
  const [ownHiddenSeries, setOwnHiddenSeries] = useState<readonly SeriesKey[]>([]);
  const hiddenSeries = controlledHiddenSeries ?? ownHiddenSeries;
  // Export skips interaction (same hard gate in build-config is convergence point for all export paths).
  const seriesInteractive = surface !== "export";
  // Remember what this viewer hid in a report chart so their own export matches their screen (see
  // `export-hidden-series.ts`). Only the chart's own legend state: the inspection layer passes controlled state.
  useEffect(() => {
    if (!sectionId || chromeless || !seriesInteractive || controlledHiddenSeries !== undefined) return;
    setExportHiddenSeries(sectionId, ownHiddenSeries);
    // The record lives only as long as this chart: once it unmounts (switched to a table locally, section removed)
    // the export draws what the screen now shows, so the stale hidden set must go.
    return () => setExportHiddenSeries(sectionId, []);
  }, [chromeless, controlledHiddenSeries, ownHiddenSeries, sectionId, seriesInteractive]);
  const toggleSeries = useCallback((key: SeriesKey) => {
    focus?.closeDetails?.();
    if (onToggleSeries) {
      onToggleSeries(key);
      return;
    }
    setOwnHiddenSeries((prev) => toggleSeriesKey(prev, key));
  }, [onToggleSeries, focus]);
  const shapeGap = useMemo(() => spec._export_analytical ? null : checkKindShape(spec, spec.kind), [spec]);
  const config = useMemo(() => {
    if (shapeGap) return null;
    try {
      return buildChartJSConfig(spec, theme, {
        visibleShareLabel: t("visible_share"),
        axisDomains,
        activeAnnotationId: chromeless ? undefined : focus?.pinned?.annotationId,
        surface,
        captionText,
        locale,
        renderContext,
        resolvedPresentation: frozenPresentation,
        // Exports receive the exporting user's hidden series from the export payload (controlled prop).
        hiddenSeries,
        onToggleSeries: seriesInteractive ? toggleSeries : undefined,
        focusBinding: focus ? { descriptor: focus.binding, datasetDigest: focus.datasetDigest, scopeSource: focus.scopeSource } : undefined,
      });
    } catch (e) {
      console.error("[ChartBlock] buildChartJSConfig failed:", e);
      return null;
    }
  }, [
    t,
    spec,
    chromeless,
    focus,
    axisDomains,
    theme,
    surface,
    captionText,
    locale,
    renderContext,
    frozenPresentation,
    shapeGap,
    hiddenSeries,
    seriesInteractive,
    toggleSeries,
  ]);

  useEffect(() => {
    if (!focus?.pinned || !config?.markBindings) return;
    const measure = focus.pinned.measureKey;
    const indices = config.markBindings.flatMap((marks, index) => marks.some((mark) => mark.observations.some((item) => item.measureKey === measure)) ? [index] : []);
    if (indices.length && indices.every((index) => (config.data.datasets[index] as { hidden?: boolean }).hidden)) focus.closeDetails?.();
  }, [config, focus]);

  // Batch 2 §3.3: reference lines are per-chart plugin, merged with build-config plugins (pie-center number).
  // Labels follow column conventions + in-chart compact mode: percent columns must show %, billion-level amounts like axes show
  // "18.2亿", never precision-mode "1,823,356,460" squeezed back into chart.
  const refLinePlugin = useMemo(() => {
    if (!refLines?.length) return null;
    const numCol = (spec.columns ?? []).find((c) => c.type === "numeric");
    // ⚠ Use mode **actually used in chart**, never recompute from spec. Mixed-mode same axis
    // `unifyPercentScalePerAxis` scales fraction columns ×100 to points before drawing; recomputing from spec
    // still yields fraction ⇒ reference line positioned correctly, label scaled again (mean 13.07 labeled 1307.0%).
    // build-config attaches column mode to dataset `_fmtCtx`; read directly here.
    const builtScale = (config?.data?.datasets ?? [])
      .map((d) => (d as { _fmtCtx?: { percent?: "fraction" | "points" | null } })._fmtCtx?.percent)
      .find((p): p is "fraction" | "points" => p === "fraction" || p === "points") ?? null;
    const pctScale = builtScale
      ?? (numCol && resolvePercentColumn(numCol)
        ? resolvePercentScale((spec.rows ?? []).map((r) => r[numCol.key]), numCol)
        : null);
    const unit = numCol?.format === "currency" && !numCol.unit ? "¥" : numCol?.unit;
    return createRefLinePlugin(refLines, {
      theme,
      format: (v) => (pctScale ? formatPercentCell(v, pctScale) : formatCompactValue(v, unit)),
      partialNote: t("ref_line_partial"),
    });
  }, [refLines, spec.columns, spec.rows, theme, t, config]);
  const focusRenderRef = useRef<{ marks: MarkBinding[][]; selection: FocusSelection | null; ink: string }>({ marks: [], selection: null, ink: theme.ink });
  focusRenderRef.current = { marks: config?.markBindings ?? [], selection: focus?.selection ?? null, ink: theme.ink };
  // react-chartjs-2 registers plugins only at construction. A stable plugin reads
  // current local state through the ref, so selection never needs an instance rebuild.
  const focusPlugin = useMemo(() => focusHighlightPlugin(() => focusRenderRef.current), []);
  const chartPlugins = useMemo(
    () => [...(config?.plugins ?? []), ...(refLinePlugin ? [refLinePlugin] : []), ...(surface === "export" ? [] : [focusPlugin])],
    [config?.plugins, refLinePlugin, surface, focusPlugin],
  );

  // Force chart instance update on theme change (no destroy/recreate, preserving zoom state)
  useEffect(() => {
    if (canvasSupported && chartRef.current) chartRef.current.update("none");
  }, [canvasSupported, theme]);

  // D1 draws chart text in the loaded interface font (`--font-v2-sans`, a swap-loaded web font). Canvas text is
  // painted once: a chart drawn before the font arrived keeps the fallback face — and label widths measured with it —
  // until something else redraws it. Redraw (and re-measure) once loading settles.
  // A face only canvas uses is not fetched until canvas first asks for it, so load it explicitly up front.
  const chartFontFamily = theme.fontSans;
  useEffect(() => {
    if (!canvasSupported || typeof document === "undefined" || !document.fonts || !chartFontFamily) return;
    let alive = true;
    const redraw = () => {
      if (alive && chartRef.current) chartRef.current.update("none");
    };
    const faces = ["400", "500", "600", "650"].map((weight) =>
      document.fonts.load(`${weight} 12px ${chartFontFamily}`).catch(() => []));
    void Promise.all(faces).then(redraw);
    return () => {
      alive = false;
    };
  }, [canvasSupported, chartFontFamily]);

  // P6/D7: mobile horizontal swipe — many-category charts (waterfall/bars/heatmap) become unreadable at 412px; derive
  // min-width from category count (force only >6), scrolling .v2-chart-canvas-wrap horizontally (no desktop regression).
  const nCats = (() => {
    if (Array.isArray(config?.data?.labels)) return config.data.labels.length;
    // Heat/matrix has no top-level labels: datasets[].data = [{x,y,v}] → count distinct x dimensions
    const pts = config?.data?.datasets?.[0]?.data as unknown;
    if (Array.isArray(pts) && pts.length && pts[0] && typeof pts[0] === "object" && "x" in pts[0]) {
      return new Set((pts as Array<{ x: unknown }>).map((p) => p.x)).size;
    }
    return 0;
  })();
  // Batch 3 4.2 (design C v2 C3): handle three wrong-axis min-width cases separately —
  //  · hbar (indexAxis="y") has vertical categories: extra width only lengthens bars, still crowds names; **grow height**
  //  · scatter/matrix lacks category axis: widening creates pointless horizontal scrolling
  //  · Others (x categories) retain min-width horizontal scrolling
  const isHorizontal = config?.options?.indexAxis === "y";
  const noCatAxis = config?.type === "scatter" || config?.type === "matrix";
  const swipeMinW = contextRecipe
    ? contextRecipe.minimumPlotWidthPx
    : (!isHorizontal && !noCatAxis && nCats > 6 ? Math.min(900, nCats * 60) : 0);
  // Horizontal bars grow height by category count. **Ceiling removed**: container now scrolls internally (`.is-tall`), readable at any height;
  // capping only flattens bars. Measured minimum 34px/category = bar + gap + one-line category name.
  //
  // ⚠ 116 device (2026-09-02): value **calculated but unused** — only
  // three `.v2-insight-card` rules read `--chart-h`, none on ordinary chart surface. Thus 30 bars squeezed into
  // 296px = **9.9px per bar**, overlapping left-side store names beyond recognition.
  // Another implemented-but-ineffective case. Three related cases resolved: `variant="quiet"` wired 2026-09-02,
  // `ChartDrill` / `rich_table` removed 2026-09-03 after confirming zero producers.
  //
  // ⚠ **Export never grows height**: contractual 1600×900 export page with `overflow:hidden` would
  // **crop** overflow in PDF — silently omitting bars is worse than crowding.
  // Long horizontal bars on export are cut to the rows that fit, with the truncation subcaption, before they get
  // here (`fitHbarRowsForExport` in ChartBlock).
  //
  // D3 (Chart Embed Spec): every horizontal bar chart is as tall as its rows (`n × pitch + axis`, pitch 40 report /
  // 36 embed and compact; `minimumPlotHeightPx`), so two or three bars are no longer stretched into slabs by the fixed
  // 300–420px box; `is-fit` drops that box's minimum. The inspection layer (`chrome="none"`) sizes its own plot area
  // and only takes the grown height for long charts, as before.
  // The value-axis title row (unit at the axis end) is budgeted on top of the rows, so it never eats the pitch.
  const valueAxisTitled = isHorizontal
    && Boolean((config?.options?.scales as { x?: { title?: { display?: boolean } } } | undefined)?.x?.title?.display);
  const recipeH = contextRecipe?.minimumPlotHeightPx
    ? contextRecipe.minimumPlotHeightPx + (valueAxisTitled ? HBAR_AXIS_TITLE_HEIGHT_PX : 0)
    : (isHorizontal && nCats > 6 ? Math.max(300, 60 + nCats * 34) : 0);
  const tallH = surface === "export" || (chromeless && nCats <= 6) ? 0 : recipeH;
  const fitH = Boolean(tallH) && isHorizontal;

  // Batch 3 4.2 (design C v2 C2): horizontal scrolling hides value axis, but must not lose magnitude; permanent chart-top
  // summary (max/min/total, precision mode). Pinned value axis rejected: Chart.js draws axes on single canvas;
  // dual-canvas split has unjustified cost/benefit (tech-design §4.2).
  // Only on mobile with genuine horizontal scrolling; desktop sees everything in one screen, summary would be noise.
  const summary = useMemo(() => {
    if (contextRecipe?.widthClass !== "compact" || !swipeMinW) return null;
    const numCol = spec.columns?.find((c) => c.type === "numeric");
    if (!numCol) return null;
    const values = (spec.rows ?? [])
      .map((r) => r[numCol.key])
      .filter((v): v is number => typeof v === "number" && !Number.isNaN(v));
    if (values.length < 2) return null;
    // review R1-7③: fewer available rows than row_count means total sums only partial data.
    // PRD §3.4.1 explicitly forbids silently showing partial-data totals — show extrema only, no total.
    const partial = (spec.rows ?? []).length < (spec.row_count ?? 0);
    const unit = numCol.format === "currency" && !numCol.unit ? "¥" : numCol.unit;
    // Summary uses **compact mode** (design C2 "最大 ¥9.8亿"), magnitude aid matching axis
    // ticks; precision-mode long numbers turn three parts into three lines at 412px, defeating instant magnitude reading.
    const compact = (v: number) => {
      const s = abbrTick(v);
      if (!unit) return s;
      return unit === "¥" || unit === "$" || unit === "€" ? `${unit}${s}` : `${s}${unit}`;
    };
    const items = [
      { label: `${numCol.header} ${t("summary_max")}`, value: compact(Math.max(...values)) },
      { label: t("summary_min"), value: compact(Math.min(...values)) },
    ];
    // Ratio columns get no total — summing refund rates fabricates numbers (same as view-transforms);
    // likewise incomplete data gets none (prefer omission over error).
    if (!resolvePercentColumn(numCol) && !partial) {
      items.push({ label: t("summary_total"), value: compact(values.reduce((a, b) => a + b, 0)) });
    }
    return items;
  }, [contextRecipe?.widthClass, swipeMinW, spec.columns, spec.row_count, spec.rows, t]);

  // review R5-2: two fallbacks (no canvas / unbuildable config) formerly neither passed chromeless
  // nor memoized spec. Inspection **often takes this path**: VIEW_KINDS includes all kinds, while
  // combo / dual_axis / scatter throw for one numeric column (`≥2 numeric`) → config
  // null → table renders own header in layer, precisely duplication chromeless eliminates.
  // ★ Must precede both early returns below — hook-order failed twice this sprint (batch 3 summary,
  //   batch 2 reference lines); chart-block.test.tsx has deterministic guard.
  const fallbackTableSpec = useMemo(
    () => ({ ...spec, kind: "table" } as ChartSpec),
    [spec],
  );

  // Fallback tables are the exact values already: no disclosure next to them, no frame of their own in an embed, and
  // the host-enabled kind switch stays reachable in the host slot.
  const fallbackFrameless = chrome !== "report";
  const fallbackSwitcher = fallbackFrameless ? undefined : switcher;
  const fallbackEmbedSwitch = chrome === "embed" && switcher && surface !== "export"
    ? <EmbedControlsSlot headerSlot={headerSlot}><div className="v2-chart-actions">{switcher}</div></EmbedControlsSlot>
    : null;
  if (!canvasSupported) {
    return <>{fallbackEmbedSwitch}<ChartFallbackTable spec={fallbackTableSpec} sourceKind={spec.kind} reason="no_canvas" switcher={fallbackSwitcher} surface={surface} sectionId={sectionId} chromeless={fallbackFrameless} /></>;
  }

  if (!config) {
    return (
      <>
        {fallbackEmbedSwitch}
        <ChartFallbackTable
          spec={fallbackTableSpec}
          sourceKind={spec.kind}
          reason={shapeGap ? "shape_mismatch" : "config_error"}
          gap={shapeGap}
          switcher={fallbackSwitcher}
          surface={surface}
          sectionId={sectionId}
          chromeless={fallbackFrameless}
        />
      </>
    );
  }

  const downloadPNG = () => {
    windowScope?.beforeCapture?.();
    track("bowhead_chart_action_total", { action: "download_png", kind: spec.kind });
    const inst = chartRef.current;
    if (!inst) return;
    exportChartPNG(inst.canvas as HTMLCanvasElement, windowScope ? `${spec.title || "chart"}_${windowScope.filenameSuffix}` : spec.title || "chart", windowScope?.label);
  };
  const downloadCSV = () => {
    windowScope?.beforeCapture?.();
    track("bowhead_chart_action_total", { action: "download_csv", kind: spec.kind });
    if (windowScope) {
      exportChartCSV(spec, `${spec.title || "chart"}_${windowScope.filenameSuffix}`);
    } else if (spec.data_artifact_id && chartDatasetAvailable(spec)) {
      window.open(
        appPath(`/api/v1/files/${encodeURIComponent(spec.data_artifact_id)}`),
        "_blank",
      );
    } else {
      exportChartCSV(spec, spec.title || "chart");
    }
  };

  const chartKey = [
    spec.kind,
    JSON.stringify(spec._export_annotations ?? []),
    config.type,
    // Batch 2 §3.3: Chart.js plugins determined at construction; react-chartjs-2 new
    // plugins prop **does not** reregister (device acceptance: checked reference line produced no change).
    // Include reference lines in key to force instance rebuild. Inspection toggles infrequent, so rebuilding acceptable.
    refLines?.length
      ? `ref:${refLines.map((r) => [
          r.kind,
          r.value ?? "",
          r.label ?? "",
          r.partial ? "partial" : "full",
          r.partialNote ?? "",
        ].join(":")).join("|")}`
      : "",
    // Chart.js does not replace registered per-chart plugins. Theme/caption changes require instance rebuild,
    // otherwise donut-center text and reference-line backdrop retain previous light/dark closures.
    `plugin-theme:${theme.ink}:${theme.inkSoft}:${theme.surface}`,
    `caption:${captionText ?? ""}`,
    config.data?.labels ? JSON.stringify(config.data.labels) : "",
    config.data?.datasets?.map((ds) => `${ds.label ?? ""}:${JSON.stringify(ds.data ?? [])}`).join("|") ?? "",
  ].join(":");

  // `embed` hosts may opt in to the kind switch and actions; they render into the host's title-row slot (or, without
  // one, in a bare row above the plot) — the chart never builds its own title row there.
  const embedControls = chrome !== "embed" || surface === "export"
    ? null
    : showEmbedActions
      ? (
        <TableActions surface={surface} variant="quiet" moreLabel={t("more_actions")} actions={chartActions()}>
          {switcher}
        </TableActions>
      )
      : switcher
        ? <div className="v2-chart-actions">{switcher}</div>
        : null;

  function chartActions() {
    return [
      {
        key: "open-data",
        label: t("open_data"),
        onClick: () => {
          track("bowhead_chart_action_total", { action: "open_data", kind: spec.kind });
          requestCanvasDataFocus(spec.data_artifact_id!, {
            entryPoint: surface === "chat" ? "chat" : "report",
            reportContext: surface === "chat"
              ? undefined
              : { label: spec.title, sectionId },
          });
        },
        hidden: !spec.data_artifact_id,
      },
      { key: "download-png", label: t("download_png"), onClick: downloadPNG },
      { key: "download-csv", label: t("download_csv"), onClick: downloadCSV },
      // Batch 2 §3.1: inspection entry. Report charts stay quiet; this button is sole
      // deep-inspection switch (TableActions centralizes export exclusion, no separate hidden flag).
      {
        key: "inspect",
        label: tInspect("open"),
        onClick: () => {
          track("bowhead_chart_inspect_total", { action: "open" });
          inspectTriggerRef.current = document.activeElement as HTMLElement | null;
          windowScope?.beforeCapture?.();
          setInspecting(true);
        },
      },
    ];
  }

  return (
    <>
    {/* canvas-sections: chart = bare figure (no white card frame), small caption + chart body + (source figcaption rendered by NodeView). */}
    <figure
      className="v2-chart-figure"
      role="figure"
      aria-label={labelledBy ? undefined : spec.title}
      aria-labelledby={labelledBy}
      data-chart-chrome={chrome}
      data-chart-output={renderContext.output.static ? "static" : renderContext.output.medium}
      data-chart-placement={renderContext.placement}
      data-chart-width-class={contextRecipe?.widthClass}
      data-chart-density={contextRecipe?.densityTier}
      data-chart-animation={contextRecipe ? (contextRecipe.animate ? "on" : "off") : undefined}
      data-chart-legend-position={contextRecipe?.legendPosition}
      style={{ margin: 0 }}
    >
      {embedControls ? <EmbedControlsSlot headerSlot={headerSlot}>{embedControls}</EmbedControlsSlot> : null}
      {chrome !== "report" ? null : (
      <div className="v2-chart-header">
        <div className="v2-chart-title" style={{ color: theme.ink }}>{spec.title}</div>
        {spec.subtitle ? (
          <div className="v2-chart-subtitle" style={{ color: theme.inkSoft }}>{spec.subtitle}</div>
        ) : null}
        {/* ⚠ Hide **actions only, preserving title**: `spec.title` exists only in this header;
          * PDF body has no second copy (116 pdftotext found only same-name insight-card
          * phrase, without "（含同比）"). Full chromeless would lose export chart title.
          * Export-action exclusion criterion lives in TableActions; see comments there. */}
        <TableActions
          surface={surface}
          variant="quiet"
          moreLabel={t("more_actions")}
          actions={chartActions()}
        >
          {switcher}
        </TableActions>
      </div>
      )}
      {summary ? (
        <div className="v2-chart-summary" style={{ color: theme.inkSoft }}>
          {summary.map((item) => (
            <span key={item.label}>{item.label} {item.value}</span>
          ))}
        </div>
      ) : null}
      {/* Only `is-tall` makes CSS read `--chart-h`. Scope by class rather than unconditional
          `.v2-chart-canvas-wrap` height/overflow change — 99% need no extra height;
          `overflow-y:auto` would add unnecessary scrollbars. */}
      <div
        ref={plotRef}
        tabIndex={focus?.select ? 0 : undefined}
        role={focus?.select ? "group" : undefined}
        aria-label={focus?.select ? `${spec.title}. ${t("focus_keyboard")}` : undefined}
        onKeyDown={focus?.select ? (event) => {
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (focus.pinned) focus.closeDetails?.(); else focus.select?.(null); return; }
          if (event.key === "Enter" || event.key === " ") {
            const mark = (config.markBindings ?? []).flat().find((item) => focus.selection && tupleKey(item.focusTuple) === tupleKey(focus.selection.tuple));
            const observation = mark?.observations.find((item) => item.value !== null);
            if (mark && observation) { event.preventDefault(); focus.select?.(mark.focusTuple, chromeless ? undefined : { kind: "point", tuple: observation.keyTuple, measureKey: observation.measureKey }); }
            return;
          }
          if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
          const marks = (config.markBindings ?? []).flat().filter((mark) => mark.observations.some((observation) => observation.value !== null));
          const unique = [...new Map(marks.map((mark) => [tupleKey(mark.focusTuple), mark])).values()];
          if (!unique.length) return;
          event.preventDefault();
          const current = focus.selection ? unique.findIndex((mark) => tupleKey(mark.focusTuple) === tupleKey(focus.selection!.tuple)) : -1;
          const step = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
          focus.select?.(unique[(current < 0 ? 0 : (current + step + unique.length) % unique.length)].focusTuple);
        } : undefined}
        className={`v2-chart-canvas-wrap${tallH ? " is-tall" : ""}${fitH ? " is-fit" : ""}${fitH && nCats <= 12 && tallH <= 960 && !renderContext.output.static ? " is-page-height" : ""}`}
        style={tallH ? ({ "--chart-h": `${tallH}px` } as CSSProperties) : undefined}
      >
        <div
          className="v2-chart-canvas-inner"
          style={swipeMinW ? ({ "--chart-min-w": `${swipeMinW}px` } as CSSProperties) : undefined}
        >
          <ReactChart
            key={chartKey}
            ref={chartRef}
            role="img"
            aria-label={spec.title}
            type={config.type}
            data={config.data}
            options={focus?.select ? { ...config.options, onClick: (event, _elements, chart) => {
              const annotationId = annotationAtPoint(chart, event.x, event.y);
              const annotation = spec._export_annotations?.find((item) => item.id === annotationId);
              if (!chromeless && annotation && focus.inspectAnnotation) {
                plotRef.current?.focus({ preventScroll: true });
                focus.inspectAnnotation(annotation); return;
              }
              const rangeHit = analyticalRangeHit(chart, spec, event.x, event.y);
              const elements = rangeHit ? [rangeHit] : event.native ? chart.getElementsAtEventForMode(event.native, "nearest", { intersect: true }, false) : [];
              const element = elements.find((item) => config.markBindings?.[item.datasetIndex]?.[item.index]?.observations.some((observation) => observation.value !== null));
              if (element) {
                const mark = config.markBindings![element.datasetIndex][element.index];
                const observation = mark.observations.find((item) => item.value !== null)!;
                plotRef.current?.focus({ preventScroll: true });
                focus.select?.(mark.focusTuple, chromeless ? undefined : { kind: "point", tuple: observation.keyTuple, measureKey: observation.measureKey });
              }
            } } : config.options}
            plugins={chartPlugins}
            updateMode={focus ? "none" : undefined}
            redraw={false}
          />
        </div>
      </div>
      {focus?.select && surface !== "export" ? <p className="chart-keyboard-hint text-xs text-muted-foreground" aria-hidden="true">{t("focus_keyboard")}</p> : null}
      {config.heatmapLegend ? <HeatmapLegendStrip legend={config.heatmapLegend} /> : null}
      {inspecting ? (
        <ChartInspect
          spec={spec}
          captionText={captionText}
          onClose={closeInspect}
          returnFocusRef={inspectTriggerRef}
        />
      ) : null}
      {process.env.NODE_ENV !== "production" && spec._infer_reason ? (
        <div className="v2-chart-debug" style={{ color: theme.inkSoft }}>
          infer: {spec._infer_reason}
        </div>
      ) : null}
    </figure>
    {/* D5: only a drawn chart offers exact values — table forms and fallbacks already are the exact values. */}
    {hasExactValues && surface !== "export" ? <ChartExactValues spec={spec} labels={exactValueLabels} /> : null}
    </>
  );
}

function AnalyticalSummaryCaptions({ spec }: { spec: ChartSpec }) {
  const locale = useLocale();
  return <div data-chart-analytical-summaries>{analyticalSummaries(spec, locale).map((item) => <p key={tupleKey([item.series])} style={SUBCAPTION_STYLE}>{item.text}</p>)}</div>;
}
