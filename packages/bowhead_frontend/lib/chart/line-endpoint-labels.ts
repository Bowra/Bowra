import { layouts, type Chart, type LayoutItem, type Plugin } from "chart.js";

import { placePointLabels } from "./point-label-placement";

export interface EndpointLabelStyle {
  font: string;
  ink: string;
  minimumPlotWidth: number;
  priority: number;
}

type EndpointDataset = Chart["data"]["datasets"][number] & {
  _endpointLabelStyle?: EndpointLabelStyle;
  datalabels?: Record<string, unknown>;
};

export function lastFiniteObservation(values: readonly unknown[]): number {
  return values.findLastIndex((value) => {
    const observed = value && typeof value === "object" && "y" in value ? value.y : value;
    return typeof observed === "number" && Number.isFinite(observed);
  });
}

/** A stale series endpoint names its actual period, never the final category's period. */
export function endpointName(name: string, index: number, labels: readonly unknown[]): string {
  return index < labels.length - 1 ? `${name} · ${String(labels[index] ?? "")}` : name;
}

/** Reserve a small gutter only when it leaves a useful plot. Long names retain the native legend. */
export function endpointGutterWidth(chartWidth: number, availableWidth: number, measuredWidth: number, minimumPlotWidth = 160): number {
  const budget = Math.min(chartWidth * 0.24, chartWidth - minimumPlotWidth - 96, availableWidth, 180);
  return measuredWidth > 0 && measuredWidth + 22 <= budget ? Math.ceil(measuredWidth) + 22 : 0;
}

/** Native Chart.js layout item; current dataset metadata survives react-chartjs-2 updates. */
export function lineEndpointLabelsPlugin(): Plugin {
  const boxes = new WeakMap<Chart, LayoutItem>();
  const displays = new WeakMap<object, unknown>();

  const observations = (chart: Chart) => chart.getSortedVisibleDatasetMetas().flatMap((meta) => {
    const dataset = chart.data.datasets[meta.index] as EndpointDataset;
    const style = dataset._endpointLabelStyle;
    const index = lastFiniteObservation(dataset.data);
    return style && index >= 0 ? [{ meta, dataset, style, index,
      text: endpointName(dataset.label ?? "", index, chart.data.labels ?? []) }] : [];
  }).sort((left, right) => left.style.priority - right.style.priority || left.meta.index - right.meta.index);

  return {
    id: "bowheadLineEndpointNames",
    beforeInit(chart) {
      if (!chart.data.datasets.some((dataset) => (dataset as EndpointDataset)._endpointLabelStyle)) return;
      const box: LayoutItem & { options: Record<string, unknown> } = {
        options: {}, position: "right", weight: 0, fullSize: false,
        width: 0, height: 0, left: 0, right: 0, top: 0, bottom: 0,
        isHorizontal: () => false, draw: () => undefined,
        update(maxWidth, maxHeight) {
          chart.ctx.save();
          const widths = observations(chart).map(({ text, style }) => {
            chart.ctx.font = style.font;
            return endpointGutterWidth(chart.width, maxWidth, chart.ctx.measureText(text).width, style.minimumPlotWidth);
          });
          chart.ctx.restore();
          this.width = Math.max(0, ...widths);
          this.height = maxHeight;
        },
      };
      boxes.set(chart, box);
      layouts.addBox(chart, box);
    },
    beforeUpdate(chart) {
      // Metadata does not exist on the first update. Install the display policy
      // from source datasets before controllers and datalabels resolve it.
      chart.data.datasets.forEach((rawDataset) => {
        const dataset = rawDataset as EndpointDataset;
        if (!dataset._endpointLabelStyle) return;
        const index = lastFiniteObservation(dataset.data);
        const labels = dataset.datalabels;
        if (!labels) return;
        if (!displays.has(labels)) displays.set(labels, labels.display);
        const original = displays.get(labels);
        labels.display = (context: { dataIndex: number }) => {
          // The endpoint remains name-only even when the gutter cannot fit: its legend and hover still identify it.
          if (context.dataIndex === index) return false;
          return typeof original === "function" ? original(context) : original;
        };
      });
    },
    afterDatasetsDraw(chart) {
      const box = boxes.get(chart);
      if (!box || box.width === 0) return;
      const ctx = chart.ctx;
      ctx.save();
      const items = observations(chart).flatMap((item) => {
        const point = item.meta.data[item.index] as { x: number; y: number; skip?: boolean } | undefined;
        if (!point || point.skip || !Number.isFinite(point.x) || !Number.isFinite(point.y)) return [];
        ctx.font = item.style.font;
        const width = ctx.measureText(item.text).width;
        return width + 22 <= box.width ? [{ ...item, point, width }] : [];
      });
      const placed = placePointLabels(items.map((item) => ({
        id: String(item.meta.index), x: box.left,
        y: Math.max(box.top + 8, Math.min(box.bottom - 8, item.point.y)),
        width: item.width, height: 16, offset: 8, keep: false,
      })), {
        fills: [{ left: 0, right: box.left, top: box.top, bottom: box.bottom }],
        labels: [], markers: [],
      }, { left: box.left, right: box.right - 4, top: box.top, bottom: box.bottom });
      placed.forEach((placement, index) => {
        if (!placement.box || placement.align !== "right") return;
        const item = items[index]!;
        const y = (placement.box.top + placement.box.bottom) / 2;
        ctx.font = item.style.font;
        ctx.strokeStyle = typeof item.dataset.borderColor === "string" ? item.dataset.borderColor : item.style.ink;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(item.point.x + 4, item.point.y);
        ctx.lineTo(placement.box.left - 4, y);
        ctx.stroke();
        ctx.fillStyle = item.style.ink;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(item.text, placement.box.left, y);
      });
      ctx.restore();
    },
    afterDestroy(chart) {
      const box = boxes.get(chart);
      if (box) layouts.removeBox(chart, box);
      chart.data.datasets.forEach((dataset) => {
        const labels = (dataset as EndpointDataset).datalabels;
        if (labels && displays.has(labels)) labels.display = displays.get(labels) as typeof labels.display;
      });
      boxes.delete(chart);
    },
  };
}
