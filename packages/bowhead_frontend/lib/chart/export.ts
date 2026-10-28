/** Chart export utilities: PNG (canvas.toDataURL) + CSV (from spec.rows). */

import { withAllPointLabels } from "./capture-canvas";
import type { ChartSpec } from "./types";

export function exportChartPNG(canvas: HTMLCanvasElement, filename: string, scopeLabel?: string): void {
  const out = document.createElement("canvas");
  out.width = canvas.width;
  const padding = scopeLabel ? 36 : 0;
  out.height = canvas.height + padding;
  const ctx = out.getContext("2d");
  if (!ctx) return;
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, out.width, out.height);
  // The image has no tooltip: draw it with every point label kept (see capture-canvas.ts).
  withAllPointLabels(canvas, (source) => ctx.drawImage(source, 0, padding));
  if (scopeLabel) {
    ctx.fillStyle = "#444";
    ctx.font = "12px sans-serif";
    ctx.fillText(scopeLabel, 12, 22, Math.max(1, out.width - 24));
  }
  const url = out.toDataURL("image/png");
  triggerDownload(url, filename.endsWith(".png") ? filename : `${filename}.png`);
}

export function exportChartCSV(spec: ChartSpec, filename: string): void {
  const rows = spec.rows ?? [];
  if (rows.length === 0) return;
  const scope = spec._export_frame_scope;
  const columns = scope && !spec.columns.some((column) => column.key === scope.time_key) ? [{ key: scope.time_key, header: scope.time_key }, ...spec.columns] : spec.columns;
  const headers = columns.map((c) => c.header);
  const keys = columns.map((c) => c.key);

  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[,"\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };

  const lines = [
    headers.map(esc).join(","),
    ...rows.map((r) => keys.map((k) => esc(r[k])).join(",")),
  ];
  // Excel-friendly: add BOM
  const csv = "\ufeff" + lines.join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  triggerDownload(url, filename.endsWith(".csv") ? filename : `${filename}.csv`);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function triggerDownload(url: string, filename: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
