import type { Chart, Plugin } from "chart.js";
import type { ChartTheme } from "../chart-theme";
import type { ObservationAnnotation } from "./annotations";
import { tupleKey, type MarkBinding } from "./linked-focus";
import { placePointLabels, segmentCrossesBox, type LabelBox, type PointLabelObstacles } from "./point-label-placement";

interface Anchor { x: number; y: number }
const annotationHits = new WeakMap<Chart, Array<{ id: string; box: LabelBox }>>();

export function annotationAtPoint(chart: Chart, x: number | null, y: number | null): string | null {
  if (x === null || y === null) return null;
  return annotationHits.get(chart)?.find(({ box }) => x >= box.left && x <= box.right && y >= box.top && y <= box.bottom)?.id ?? null;
}

/** Unlike value labels, remarks must fit entirely inside the plot and yield to drawn lines. */
export function placeAnnotationLabel(anchor: Anchor, width: number, bounds: LabelBox, obstacles: PointLabelObstacles): LabelBox | null {
  for (const offset of [12, 24, 40]) {
    const box = placePointLabels([{ ...anchor, id: "annotation", width, height: 18, offset, keep: false, leader: true }], obstacles, bounds)[0]?.box;
    if (box && box.left >= bounds.left && box.right <= bounds.right && box.top >= bounds.top && box.bottom <= bounds.bottom
      && !obstacles.segments?.some((segment) => segmentCrossesBox(segment, box))) return box;
  }
  return null;
}

/** Read the installed datalabels plugin's actual rotated hit boxes, not estimates of formatted text. */
function valueLabelBoxes(chart: Chart): LabelBox[] {
  const labels = (chart as Chart & { $datalabels?: { _labels?: Array<{ $layout?: { _visible?: boolean; _box?: { _points?: () => Anchor[] } } }> } }).$datalabels?._labels ?? [];
  return labels.flatMap((label) => {
    const points = label.$layout?._visible ? label.$layout._box?._points?.() : null;
    if (!points?.length || points.some((point) => !Number.isFinite(point.x) || !Number.isFinite(point.y))) return [];
    return [{ left: Math.min(...points.map((point) => point.x)) - 3, right: Math.max(...points.map((point) => point.x)) + 3,
      top: Math.min(...points.map((point) => point.y)) - 3, bottom: Math.max(...points.map((point) => point.y)) + 3 }];
  });
}

/** Shared screen/export layout: remarks yield to the actual formatted value-label boxes. */
export function observedAnnotationLayoutPlugin(items: ObservationAnnotation[], marks: MarkBinding[][], theme: ChartTheme): Plugin {
  return { id: "nativeObservedAnnotations", afterEvent(chart, { event }) {
    chart.canvas.style.cursor = annotationAtPoint(chart, event.x, event.y) ? "pointer" : "";
  }, afterDatasetsDraw(chart) {
    const hits: Array<{ id: string; box: LabelBox }> = [];
    annotationHits.set(chart, hits);
    const activeId = (chart.options.plugins as { nativeObservedAnnotations?: { activeId?: string } } | undefined)?.nativeObservedAnnotations?.activeId;
    const area = chart.chartArea;
    const bounds = { left: area.left + 3, right: area.right - 3, top: area.top + 3, bottom: area.bottom - 3 };
    const obstacles: PointLabelObstacles = { fills: [], labels: valueLabelBoxes(chart), markers: [], segments: [] };
    const anchors = new Map<string, Anchor[]>();
    chart.data.datasets.forEach((_, datasetIndex) => {
      if (!chart.isDatasetVisible(datasetIndex)) return;
      const meta = chart.getDatasetMeta(datasetIndex);
      let previous: Anchor | null = null;
      meta.data.forEach((element, index) => {
        const mark = marks[datasetIndex]?.[index];
        const position = element.getProps(["x", "y", "width", "height", "base", "horizontal", "skip"], false) as Anchor & { width?: number; height?: number; base?: number; horizontal?: boolean; skip?: boolean };
        if (position.skip || !mark?.observations.some((value) => value.value !== null) || !Number.isFinite(position.x) || !Number.isFinite(position.y)) { previous = null; return; }
        if (meta.type === "bar" && position.base !== undefined) {
          obstacles.fills.push(position.horizontal
            ? { left: Math.min(position.x, position.base), right: Math.max(position.x, position.base), top: position.y - (position.height ?? 0) / 2, bottom: position.y + (position.height ?? 0) / 2 }
            : { left: position.x - (position.width ?? 0) / 2, right: position.x + (position.width ?? 0) / 2, top: Math.min(position.y, position.base), bottom: Math.max(position.y, position.base) });
        } else {
          obstacles.markers.push({ ...position, r: 6 });
          if (meta.type === "line" && previous) obstacles.segments!.push({ x1: previous.x, y1: previous.y, x2: position.x, y2: position.y });
          previous = position;
        }
        items.forEach((item) => {
          const value = mark.focusTuple[0];
          const matches = item.kind === "point" ? tupleKey(mark.focusTuple) === tupleKey(item.tuple)
            : item.id === activeId ? typeof value === typeof item.lower && value !== null && value >= item.lower && value <= item.upper
              : value === item.lower || value === item.upper;
          if (matches && mark.observations.some((observation) => observation.measureKey === item.measure_key && observation.value !== null)) {
            const points = anchors.get(item.id) ?? [];
            points.push({ x: position.x, y: position.y });
            anchors.set(item.id, points);
          }
        });
      });
    });
    const ordered = items.map((item, index) => ({ item, ordinal: index + 1 })).sort((a, b) => Number(b.item.id === activeId) - Number(a.item.id === activeId));
    const ctx = chart.ctx;
    ctx.save();
    ctx.font = `500 11px ${theme.fontSans}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    let textCount = 0;
    ordered.forEach(({ item, ordinal }) => {
      const points = anchors.get(item.id);
      if (!points?.length) return;
      ctx.strokeStyle = theme.ink;
      ctx.lineWidth = item.id === activeId ? 2 : 1;
      points.forEach((point) => { ctx.beginPath(); ctx.arc(point.x, point.y, item.id === activeId ? 8 : 6, 0, Math.PI * 2); ctx.stroke(); });
      const anchor = item.kind === "interval" ? points[points.length - 1] : points[0];
      // Active intervals highlight supplied observations; one leader ties the caption to an actual point.
      const full = `${ordinal}. ${item.label ?? item.measure_key}`;
      let text = full;
      let box = textCount < 3 ? placeAnnotationLabel(anchor, ctx.measureText(text).width + 8, bounds, obstacles) : null;
      if (!box) { text = String(ordinal); box = placeAnnotationLabel(anchor, ctx.measureText(text).width + 8, bounds, obstacles); }
      if (!box) return;
      hits.push({ id: item.id, box });
      if (text === full) textCount += 1;
      obstacles.labels.push(box);
      const x = Math.max(box.left, Math.min(anchor.x, box.right));
      const y = Math.max(box.top, Math.min(anchor.y, box.bottom));
      ctx.beginPath(); ctx.moveTo(anchor.x, anchor.y); ctx.lineTo(x, y); ctx.stroke();
      ctx.fillStyle = theme.ink;
      ctx.fillText(text, box.left + 4, (box.top + box.bottom) / 2);
    });
    ctx.restore();
  } };
}
