import type { Chart, ChartData, ChartOptions, Plugin } from "chart.js";
import type { ChartTheme } from "../chart-theme";
import type { ChartJSConfig } from "./build-config";
import type { ChartSpec } from "./types";
import { analyticalX, analyticalValue, type AnalyticalRole } from "./analytical";
import { tupleKey, type MarkBinding, type TypedCell } from "./linked-focus";
import { seriesColor } from "./color";
import { placePointLabels } from "./point-label-placement";

type Row = Record<string, TypedCell>;
/** A supplied range remains inspectable when its independent value observation is missing. */
export function analyticalRangeHit(chart: Chart, spec: ChartSpec, x: number | null, y: number | null) {
  const a = spec._export_analytical;
  if (!a?.range || a.preset !== "series_plot" || x == null || y == null || !chart.scales.x || !chart.scales.y) return null;
  for (const [datasetIndex, dataset] of chart.data.datasets.entries()) {
    if (!chart.isDatasetVisible(datasetIndex)) continue;
    const rows = (dataset as unknown as { _analyticalRows?: Row[] })._analyticalRows ?? [];
    for (const [index, row] of rows.entries()) {
      const low = row[a.range.lower_key], high = row[a.range.upper_key];
      if (typeof low !== "number" || typeof high !== "number") continue;
      const px = chart.scales.x.getPixelForValue(analyticalX(row[a.x_key!], a.x_kind!));
      const lower = chart.scales.y.getPixelForValue(low), upper = chart.scales.y.getPixelForValue(high);
      if (Math.abs(px - x) <= 8 && y >= Math.min(lower, upper) - 6 && y <= Math.max(lower, upper) + 6) return { datasetIndex, index };
    }
  }
  return null;
}
export function analyticalSeriesStyle(role: AnalyticalRole | undefined, index: number, theme: ChartTheme) {
  const color = role === "observed" || role === "reference" ? theme.inkSoft : role === "current" ? theme.ink : seriesColor(theme.palette, role === "optimistic" ? 1 : role === "pessimistic" ? 2 : role === "baseline" || role === "primary" ? 0 : index, theme.surface, theme.ink);
  return { borderColor: color, backgroundColor: color, borderWidth: role === "primary" || role === "baseline" ? Math.max(2.5, theme.lineWidth ?? 2) : theme.lineWidth ?? 2,
    borderDash: role === "optimistic" ? [7, 4] : role === "pessimistic" ? [3, 4] : role === "reference" ? [2, 3] : [],
    pointRadius: role === "current" ? 5 : 2, pointStyle: role === "reference" ? "rectRot" as const : "circle" as const,
    showLine: role !== "current", tension: 0, cubicInterpolationMode: "default" as const, spanGaps: false };
}

export function analyticalSummaries(spec: ChartSpec, locale: string) {
  const a = spec._export_analytical;
  if (!a || a.preset !== "series_plot" || !a.summary_keys?.length) return [];
  return (a.binding.series_values ?? []).flatMap((series) => {
    const rows = (spec.rows ?? []).filter((row) => tupleKey([row[a.series_key!]]) === tupleKey([series]));
    const fields = a.summary_keys!.flatMap((key) => {
      const row = rows.find((candidate) => candidate[key] != null);
      return row ? [`${spec.columns.find((column) => column.key === key)?.header ?? key}: ${analyticalValue(spec, key, row[key], locale, false)}`] : [];
    });
    return fields.length ? [{ series, text: `${String(series)} · ${fields.join(" · ")}`, label: `${String(series)} · ${a.summary_keys!.flatMap((key) => { const row = rows.find((candidate) => candidate[key] != null); return row ? [analyticalValue(spec, key, row[key], locale, false)] : []; }).join(" · ")}` }] : [];
  });
}

/** Each dataset keeps its own original x positions; hover joins only an exact x. */
export function buildAnalyticalSeries(spec: ChartSpec, theme: ChartTheme, options: ChartOptions, locale = "en-US", digest = ""): ChartJSConfig {
  const a = spec._export_analytical!;
  const valueColumn = spec.columns.find((column) => column.key === a.value_key)!;
  const seriesValues = a.binding.series_values!;
  const bySeries = seriesValues.map((series) => (spec.rows ?? []).filter((row) => tupleKey([row[a.series_key!]]) === tupleKey([series])).sort((l, r) => analyticalX(l[a.x_key!], a.x_kind!) - analyticalX(r[a.x_key!], a.x_kind!)));
  const datasets = bySeries.map((rows, index) => {
    const series = seriesValues[index];
    const role = a.series_roles?.find((item) => tupleKey([item.series_value]) === tupleKey([series]))?.role;
    return { label: String(series), data: rows.map((row) => ({ x: analyticalX(row[a.x_key!], a.x_kind!), y: row[a.value_key!] })),
      ...analyticalSeriesStyle(role, index, theme), _analyticalStyle: analyticalSeriesStyle(role, index, theme), _analyticalRows: rows,
      _seriesKey: `analytical:${JSON.stringify(series)}`, _yCol: valueColumn, pointHoverRadius: 5, hitRadius: 8,
      datalabels: { display: false } };
  });
  const marks: MarkBinding[][] = bySeries.map((rows) => rows.map((row) => ({ focusTuple: a.binding.observation_keys.map((key) => row[key]), representation: "original",
    observations: a.binding.measure_keys.map((key) => ({ datasetDigest: digest, keyTuple: a.binding.observation_keys.map((field) => row[field]), measureKey: key, value: typeof row[key] === "number" ? row[key] as number : null })) })));
  options.interaction = { mode: "nearest", axis: "xy", intersect: false };
  if (options.plugins?.tooltip) {
    options.plugins.tooltip.mode = "nearest";
    options.plugins.tooltip.axis = "xy";
    options.plugins.tooltip.intersect = false;
    options.plugins.tooltip.callbacks = {
      title: (items) => {
        const item = items[0], row = item && bySeries[item.datasetIndex]?.[item.dataIndex];
        return row ? analyticalValue(spec, a.x_key!, row[a.x_key!], locale) : "";
      },
      label: (ctx) => {
        const source = bySeries[ctx.datasetIndex]?.[ctx.dataIndex];
        if (!source) return [];
        const x = analyticalX(source[a.x_key!], a.x_kind!);
        return bySeries.flatMap((rows, index) => {
          if (!ctx.chart.isDatasetVisible(index)) return [];
          const row = rows.find((candidate) => analyticalX(candidate[a.x_key!], a.x_kind!) === x);
          if (!row) return [];
          return [`${String(seriesValues[index])}: ${analyticalValue(spec, a.value_key!, row[a.value_key!], locale)}`,
            ...(a.range && (row[a.range.lower_key] != null || row[a.range.upper_key] != null) ? [`${spec.columns.find((c) => c.key === a.range!.lower_key)?.header}: ${analyticalValue(spec, a.range.lower_key, row[a.range.lower_key], locale)} – ${spec.columns.find((c) => c.key === a.range!.upper_key)?.header}: ${analyticalValue(spec, a.range.upper_key, row[a.range.upper_key], locale)}`] : [])];
        });
      },
    };
    options.plugins.tooltip.filter = (_item, index) => index === 0;
  }
  const summaries = analyticalSummaries(spec, locale);
  const plugin: Plugin = { id: "nativeAnalyticalRanges", afterEvent(chart, args) {
    if (args.event.type === "mouseout") return;
    const hit = analyticalRangeHit(chart, spec, args.event.x, args.event.y);
    if (hit && chart.tooltip && args.event.x != null && args.event.y != null) {
      chart.tooltip.setActiveElements([hit], { x: args.event.x, y: args.event.y });
      args.changed = true;
    }
  }, afterDatasetsDraw(chart) {
    const { ctx, chartArea, scales } = chart;
    if (!scales.x || !scales.y) return;
    ctx.save(); ctx.beginPath(); ctx.rect(chartArea.left, chartArea.top, chartArea.right - chartArea.left, chartArea.bottom - chartArea.top); ctx.clip();
    const markers: Array<{ x: number; y: number; r: number }> = [];
    const requests: Array<{ id: string; x: number; y: number; width: number; height: number; offset: number; keep: boolean; leader: boolean }> = [];
    const labels: string[] = [];
    ctx.font = `11px ${theme.fontSans ?? "sans-serif"}`;
    bySeries.forEach((rows, index) => {
      if (!chart.isDatasetVisible(index)) return;
      const color = datasets[index]!.borderColor;
      ctx.strokeStyle = color; ctx.fillStyle = color;
      const points = chart.getDatasetMeta(index).data;
      points.forEach((point) => { if (Number.isFinite(point.x) && Number.isFinite(point.y)) markers.push({ x: point.x, y: point.y, r: 4 }); });
      if (a.range) for (const row of rows) {
        const low = row[a.range.lower_key], high = row[a.range.upper_key];
        if (typeof low !== "number" || typeof high !== "number") continue;
        const x = scales.x!.getPixelForValue(analyticalX(row[a.x_key!], a.x_kind!)), y1 = scales.y!.getPixelForValue(low), y2 = scales.y!.getPixelForValue(high);
        ctx.globalAlpha = 0.55; ctx.lineWidth = 3; ctx.setLineDash(a.range.origin_key && row[a.range.origin_key] === "estimated" ? [4, 3] : []);
        ctx.beginPath(); ctx.moveTo(x, y1); ctx.lineTo(x, y2); ctx.stroke();
        ctx.setLineDash([]); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x - 4, y1); ctx.lineTo(x + 4, y1); ctx.moveTo(x - 4, y2); ctx.lineTo(x + 4, y2); ctx.stroke();
      }
      const summary = summaries.find((item) => tupleKey([item.series]) === tupleKey([seriesValues[index]]));
      const end = rows.findLastIndex((row) => typeof row[a.value_key!] === "number");
      const point = points[end];
      if (summary && point && Number.isFinite(point.x) && Number.isFinite(point.y)) {
        requests.push({ id: String(index), x: point.x, y: point.y, width: ctx.measureText(summary.label).width + 4, height: 16, offset: 12, keep: false, leader: true });
        labels.push(summary.label);
      }
    });
    ctx.globalAlpha = 1; ctx.setLineDash([]);
    const placements = placePointLabels(requests, { fills: [], labels: [], markers }, chartArea);
    placements.forEach((placement, index) => {
      if (!placement.box) return;
      const request = requests[index]!;
      ctx.strokeStyle = theme.inkSoft; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(request.x, request.y); ctx.lineTo(Math.max(placement.box.left, Math.min(placement.box.right, request.x)), Math.max(placement.box.top, Math.min(placement.box.bottom, request.y))); ctx.stroke();
      ctx.fillStyle = theme.surface; ctx.fillRect(placement.box.left, placement.box.top, placement.box.right - placement.box.left, placement.box.bottom - placement.box.top);
      ctx.fillStyle = theme.ink; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillText(labels[index]!, placement.box.left + 2, (placement.box.top + placement.box.bottom) / 2);
    });
    ctx.restore();
  } };
  return { type: "line", data: { datasets } as ChartData, options, plugins: [plugin], markBindings: marks };
}
