/**
 * Image captures (copy as image, download PNG) of a live chart canvas.
 *
 * On screen a point label next to a bar may hide when nothing fits (the tooltip still has the value). A copied or
 * downloaded image has no tooltip, so for the capture the chart redraws once with every label kept (moved, never
 * hidden), the pixels are taken synchronously, and the chart redraws back.
 */

import { Chart } from "chart.js";

import { POINT_LABEL_KEEP_ALL_KEY } from "./build-config";

type ChartLike = { update: (mode?: string) => void } & Record<string, unknown>;

/** Run `draw` against `canvas` while its chart (if any) shows every point label. `draw` must be synchronous. */
export function withAllPointLabels<T>(
  canvas: HTMLCanvasElement,
  draw: (canvas: HTMLCanvasElement) => T,
  lookup: (canvas: HTMLCanvasElement) => ChartLike | undefined = (c) =>
    Chart.getChart(c) as unknown as ChartLike | undefined,
): T {
  const chart = lookup(canvas);
  if (!chart) return draw(canvas);
  chart[POINT_LABEL_KEEP_ALL_KEY] = true;
  try {
    chart.update("none");
    return draw(canvas);
  } finally {
    delete chart[POINT_LABEL_KEEP_ALL_KEY];
    chart.update("none");
  }
}

/** A detached copy of the canvas pixels with every point label kept; the canvas itself when nothing can redraw it. */
export function snapshotWithAllPointLabels(canvas: HTMLCanvasElement): HTMLCanvasElement {
  const chart = Chart.getChart(canvas);
  if (!chart) return canvas;
  return withAllPointLabels(canvas, (source) => {
    const out = document.createElement("canvas");
    out.width = source.width;
    out.height = source.height;
    out.getContext("2d")?.drawImage(source, 0, 0);
    return out;
  });
}
