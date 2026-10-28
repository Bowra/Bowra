import "./_process-shim";
import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { ReportView, type ReportViewHandle } from "../components/canvas/report-view";
import type { ReportSection } from "../lib/types";
import type { ChartSpec } from "../lib/chart/types";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";
import de from "../messages/de-DE.json";
import fr from "../messages/fr-FR.json";
import { ChartThemePreviewProvider, chartThemeBaseForMode, resolveChartThemeForStyle } from "../lib/chart-theme";
import "../styles/report-canvas.css";
import "../styles/ui-refresh-canvas.css";
import { Chart as ChartJS, PointElement, BarElement } from "chart.js";

const child: ChartSpec = {
  kind: "table", title: "Values", row_count: 2, data_artifact_id: "art_composition",
  columns: [{ key: "region", header: "Region", type: "categorical" }, { key: "value", header: "Value", type: "numeric", unit: "USD", currency_code: "USD", display: { decimal_places: 3, decimal_mode: "fixed" } }],
  rows: [{ region: "A", value: 12 }, { region: "B", value: 21 }],
};
const initial: ReportSection = {
  id: "sec_composition", kind: "chart", rev: 1,
  spec: { ...child, title: "Regional results",
    views: [{ id: "comparison", kind: "chart", spec: { ...child, title: "Comparison" } },
      { id: "details", kind: "chart", spec: child }],
    layout: { type: "split", children: [{ view: "comparison" }, { view: "details" }] } },
};

declare global {
  interface Window {
    chartCompositionHost: { rangePosition: (view: string, x: number, y: number) => { x: number; y: number } | null; configure: (options: { width?: number; locale?: "en-US" | "zh-CN" | "de-DE" | "fr-FR"; mode?: "light" | "dark"; contrast?: "standard" | "high" }) => void; companions: (sections: ReportSection[]) => void; hydrate: (section: ReportSection) => void; update: (section: ReportSection) => void; readOnly: (value: boolean) => void; embedded: (value: boolean) => void; prose: (value: string) => void; missing: (value: boolean) => void; prepareExport: () => Promise<boolean>; settleCharts: () => number; tooltipLines: (view: string) => string[]; numericTickBounds: (view: string) => Array<{ left: number; right: number; width: number }>; chartLabels: (view: string) => unknown[]; chartScale: (view: string, axis: string) => { min: number; max: number } | null; scatterPoints: (view: string) => { values: Array<{ x: number; y: number }>; settled: boolean }; seriesHidden: (view: string, index: number) => boolean; legendPosition: (view: string, index: number) => { x: number; y: number } | null; firstMarkPosition: (view: string) => { x: number; y: number } | null };
  }
}

/** Actual Canvas ReportView/NodeView/sync stack with a test-owned transport. */
function Host() {
  const [section, setSection] = useState(initial);
  const [companions, setCompanions] = useState<ReportSection[]>([]);
  const [configuration, setConfiguration] = useState({ width: 1000, locale: "en-US" as "en-US" | "zh-CN" | "de-DE" | "fr-FR", mode: "light" as "light" | "dark", contrast: "standard" as "standard" | "high" });
  const [generation, setGeneration] = useState(0);
  const [readOnly, setReadOnly] = useState(false);
  const [embedded, setEmbedded] = useState(false);
  const [prose, setProse] = useState("Before the figure");
  const [missing, setMissing] = useState(false);
  const reportRef = useRef<ReportViewHandle>(null);
  window.chartCompositionHost = {
    companions: setCompanions,
    configure: (options) => { if (options.locale) document.documentElement.lang = options.locale; setConfiguration((value) => ({ ...value, ...options })); if (options.mode) document.documentElement.dataset.theme = options.mode; },
    hydrate: (saved) => { setSection(saved); setGeneration((value) => value + 1); },
    update: setSection,
    readOnly: setReadOnly,
    embedded: setEmbedded,
    prose: setProse,
    missing: setMissing,
    prepareExport: async () => (await reportRef.current?.prepareForExport({ format: "pdf", profile: "full_report", forceLegacy: true }))?.ok ?? false,
    rangePosition: (view, x, y) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      const chart = canvas ? ChartJS.getChart(canvas) : undefined;
      return chart?.scales.x && chart?.scales.y ? { x: chart.scales.x.getPixelForValue(x), y: chart.scales.y.getPixelForValue(y) } : null;
    },
    seriesHidden: (view, index) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      return canvas ? ChartJS.getChart(canvas)?.isDatasetVisible(index) === false : false;
    },
    scatterPoints: (view) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      const chart = canvas ? ChartJS.getChart(canvas) : undefined;
      if (!chart?.scales.x || !chart.scales.y) return { values: [], settled: false };
      const values: Array<{ x: number; y: number }> = [];
      let settled = true;
      chart.data.datasets.forEach((dataset, datasetIndex) => {
        dataset.data.forEach((raw, index) => {
          if (!raw || typeof raw !== "object" || !("x" in raw) || !("y" in raw) || typeof raw.x !== "number" || typeof raw.y !== "number") return;
          values.push({ x: raw.x, y: raw.y });
          const point = chart.getDatasetMeta(datasetIndex).data[index];
          if (!(point instanceof PointElement) || Math.abs(point.x - chart.scales.x.getPixelForValue(raw.x)) > 0.5 || Math.abs(point.y - chart.scales.y.getPixelForValue(raw.y)) > 0.5) settled = false;
        });
      });
      return { values, settled };
    },
    chartScale: (view, axis) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      const scale = canvas ? ChartJS.getChart(canvas)?.scales[axis] : undefined;
      return scale ? { min: scale.min, max: scale.max } : null;
    },
    legendPosition: (view, index) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      const legend = (canvas ? ChartJS.getChart(canvas)?.legend : undefined) as unknown as { legendHitBoxes?: Array<{ left: number; top: number; width: number; height: number }> } | undefined;
      const hit = legend?.legendHitBoxes?.[index];
      return hit ? { x: hit.left + hit.width / 2, y: hit.top + hit.height / 2 } : null;
    },
    chartLabels: (view) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      return canvas ? ChartJS.getChart(canvas)?.data.labels ?? [] : [];
    },
    settleCharts: () => {
      let count = 0;
      document.querySelectorAll("canvas").forEach((canvas) => { const chart = ChartJS.getChart(canvas); if (chart) { chart.stop(); chart.update("none"); count++; } });
      return count;
    },
    tooltipLines: (view) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      const tooltip = canvas ? ChartJS.getChart(canvas)?.tooltip : undefined;
      return tooltip?.opacity === 1 ? tooltip.body.flatMap((item) => item.lines) : [];
    },
    numericTickBounds: (view) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      const chart = canvas ? ChartJS.getChart(canvas) : undefined;
      if (!chart) return [];
      return Object.values(chart.scales).filter((scale) => scale.type === "linear" && !scale.isHorizontal()).flatMap((scale) => scale.getLabelItems().map((item) => {
        chart.ctx.save(); chart.ctx.font = item.font.string;
        const width = Math.max(...(Array.isArray(item.label) ? item.label : [item.label]).map((text) => chart.ctx.measureText(String(text)).width));
        chart.ctx.restore();
        const x = item.options.translation![0];
        const left = x - (item.options.textAlign === "right" ? width : item.options.textAlign === "center" ? width / 2 : 0);
        return { left, right: left + width, width: chart.width };
      }));
    },
    firstMarkPosition: (view) => {
      const canvas = document.querySelector<HTMLCanvasElement>(`[data-chart-view="${view}"] canvas`);
      const mark = canvas ? ChartJS.getChart(canvas)?.getDatasetMeta(0).data[0] : undefined;
      const point = mark instanceof PointElement || mark instanceof BarElement ? mark.getCenterPoint() : null;
      return typeof point?.x === "number" && typeof point.y === "number" && Number.isFinite(point.x) && Number.isFinite(point.y) ? { x: point.x, y: point.y } : null;
    },
  };
  return <NextIntlClientProvider locale={configuration.locale} messages={{ "en-US": en, "zh-CN": zh, "de-DE": de, "fr-FR": fr }[configuration.locale]}>
    <ChartThemePreviewProvider theme={resolveChartThemeForStyle("bowhead-editorial", { contrast: configuration.contrast, colorVision: "standard" }, chartThemeBaseForMode(configuration.mode), configuration.mode)}>
    <main style={{ maxWidth: configuration.width, margin: "0 auto" }}>
      <ReportView ref={reportRef} key={generation} sections={embedded ? [
        { id: "sec_prose", kind: "narrative", rev: 1, markdown: `${prose}\n\n[figure:${section.id}]\n\nAfter the figure` },
        ...(missing ? [] : [section]),
      ] : [...(missing ? [] : [section]), ...companions]} canvasId="cv_composition" isEditMode canEdit readOnly={readOnly} seqAuthoritative />
    </main>
    </ChartThemePreviewProvider>
  </NextIntlClientProvider>;
}

createRoot(document.getElementById("root")!).render(<Host />);
