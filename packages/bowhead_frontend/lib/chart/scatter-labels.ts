/**
 * Which scatter points to label — greedy collision avoidance.
 *
 * ## Why this exists
 *
 * Currently only the **one** protagonist scatter point has a persistent name; others rely on hover. Hover is absent
 * in two scenarios: export (PNG / PDF / PPTX) and touch screens. Existing export recipe explicitly requires
 * no reliance on hover; **information present only in tooltips must appear in labels, legends, titles, captions,
 * or adjacent tables** — thus an export chart with 40 unnamed points is unusable under the contract.
 *
 * The consequence goes beyond aesthetics: on 116 an agent could not natively label every point with its store name,
 * so it used matplotlib to produce a PNG, bypassing the entire chart pipeline (no interaction / chart-kind switching /
 * data download / provenance continuity). See sprint document §24.5 / §26.
 *
 * ## Why we must calculate collision avoidance
 *
 * chartjs-plugin-datalabels has **no automatic collision avoidance**. Prototype's all-labels mode demonstrates this:
 * names of 19 stores in the 10K–17K area range overlap into a blur. We must calculate overlaps ourselves.
 *
 * ## Criterion
 *
 * Importance = residual from least-squares fit line (deviations from trend are the point of scatter plots),
 * with protagonist first; try label boxes in descending importance, skipping any that intersect a placed box.
 *
 * ★ **No maximum-label-count parameter.** Prototype geometry limits counts itself
 * (about 18 at 720px, 14 at 344px, both with zero overlaps). A capacity knob adds another drifting estimate.
 */

export interface LabelPoint {
  x: number;
  y: number;
  label: string;
}

/** Label position relative to point: above first, then below, right, left (see pickScatterLabels). */
export type LabelAlign = "top" | "bottom" | "right" | "left";

export interface ScatterLabelPlacement {
  picked: Set<number>;
  /** Index → label direction for that point; points outside picked are absent. */
  align: Map<number, LabelAlign>;
}

export interface PickOptions {
  /** Pixel position of point on canvas. */
  toPixel(p: LabelPoint, i: number): { px: number; py: number };
  /** Measure actual text width (ctx.measureText) — character counts fail for mixed Chinese/English. */
  measure(label: string): number;
  /** Plot area used to detect labels exceeding boundaries. */
  area: { top: number; bottom: number; left: number; right: number };
  /** Label named by the caption; always label it when present. */
  lead?: string | null;
  lineHeight?: number;
  /** Point radius + gap; distance between label and point. */
  offset?: number;
  /** Every drawn point marker in pixels (all datasets), so a label never covers another point.
   *  Readability audit (33 scatter charts): labels only avoided each other, so a picked label landed on top of the
   *  unlabelled neighbour it stood next to — the point disappeared under the name of a different store.
   *  A marker at the labelled point's own position is ignored (its label sits beside it by construction). */
  markers?: ReadonlyArray<{ px: number; py: number; r: number }>;
}

type Marker = { px: number; py: number; r: number };

/** Uniform bucket grid so each candidate box only tests nearby markers: scatter charts carry every plotted row
 *  (thousands), and testing each candidate against every marker would be quadratic on the layout path. */
const GRID_PX = 32;
function buildMarkerGrid(markers: ReadonlyArray<Marker>) {
  const cells = new Map<string, Marker[]>();
  for (const m of markers) {
    if (!Number.isFinite(m.px) || !Number.isFinite(m.py)) continue;
    const span = Math.max(0, m.r);
    for (let cx = Math.floor((m.px - span) / GRID_PX); cx <= Math.floor((m.px + span) / GRID_PX); cx += 1) {
      for (let cy = Math.floor((m.py - span) / GRID_PX); cy <= Math.floor((m.py + span) / GRID_PX); cy += 1) {
        const key = `${cx}:${cy}`;
        const bucket = cells.get(key);
        if (bucket) bucket.push(m);
        else cells.set(key, [m]);
      }
    }
  }
  return {
    near(box: { l: number; t: number; r: number; b: number }): Marker[] {
      const out: Marker[] = [];
      for (let cx = Math.floor(box.l / GRID_PX); cx <= Math.floor(box.r / GRID_PX); cx += 1) {
        for (let cy = Math.floor(box.t / GRID_PX); cy <= Math.floor(box.b / GRID_PX); cy += 1) {
          const bucket = cells.get(`${cx}:${cy}`);
          if (bucket) out.push(...bucket);
        }
      }
      return out;
    },
  };
}

/** Does an axis-aligned box intersect a circle? Closest point on the box to the centre, within the radius. */
function boxHitsCircle(box: { l: number; t: number; r: number; b: number }, m: { px: number; py: number; r: number }): boolean {
  const cx = Math.max(box.l, Math.min(m.px, box.r));
  const cy = Math.max(box.t, Math.min(m.py, box.b));
  const dx = m.px - cx;
  const dy = m.py - cy;
  return dx * dx + dy * dy < m.r * m.r;
}

const PAD = 2;

/** Candidate slots in preference order. */
export const LABEL_SLOTS: readonly LabelAlign[] = ["top", "bottom", "right", "left"];

/** Fewest names a scatter keeps when avoiding points would leave fewer (see the second pass in pickScatterLabels). */
export const LABEL_FLOOR = 12;

/** The box a label of `width` occupies in slot `dir` next to the point at (px, py), padding included.
 *  The single geometry both the picker and its tests use. */
export function labelBox(
  px: number,
  py: number,
  width: number,
  dir: LabelAlign,
  lineHeight = 12,
  offset = 6,
): { l: number; t: number; r: number; b: number } {
  const halfW = width / 2;
  const halfH = lineHeight / 2;
  switch (dir) {
    case "top":
      return { l: px - halfW - PAD, t: py - offset - lineHeight - PAD, r: px + halfW + PAD, b: py - offset + PAD };
    case "bottom":
      return { l: px - halfW - PAD, t: py + offset - PAD, r: px + halfW + PAD, b: py + offset + lineHeight + PAD };
    case "right":
      return { l: px + offset - PAD, t: py - halfH - PAD, r: px + offset + width + PAD, b: py + halfH + PAD };
    case "left":
      return { l: px - offset - width - PAD, t: py - halfH - PAD, r: px - offset + PAD, b: py + halfH + PAD };
  }
}

function residualScore(pts: readonly LabelPoint[]): (p: LabelPoint) => number {
  const n = pts.length;
  if (n < 2) return () => 0;
  let sx = 0;
  let sy = 0;
  let sxx = 0;
  let sxy = 0;
  for (const p of pts) {
    sx += p.x;
    sy += p.y;
    sxx += p.x * p.x;
    sxy += p.x * p.y;
  }
  const denom = n * sxx - sx * sx;
  const k = denom === 0 ? 0 : (n * sxy - sx * sy) / denom;
  const b = (sy - k * sx) / n;
  return (p) => Math.abs(p.y - (k * p.x + b));
}

/**
 * Select points that can be labeled without overlaps.
 *
 * ⚠ Requires **actual pixels**, so call only after Chart.js afterLayout —
 * buildChartJSConfig is pure and cannot access scales.
 */
export function pickScatterLabels(
  pts: readonly LabelPoint[],
  opts: PickOptions,
): ScatterLabelPlacement {
  const lineHeight = opts.lineHeight ?? 12;
  const offset = opts.offset ?? 6;
  const score = residualScore(pts);
  const order = pts
    .map((p, i) => [i, p.label === opts.lead ? Number.POSITIVE_INFINITY : score(p)] as const)
    .sort((a, b) => b[1] - a[1]);

  const markerGrid = buildMarkerGrid(opts.markers ?? []);
  const placed: Array<{ l: number; t: number; r: number; b: number }> = [];
  const picked = new Set<number>();
  const align = new Map<number, LabelAlign>();

  // Pass 1 avoids labels and points. Pass 2 only runs when that leaves fewer than `LABEL_FLOOR` names — a cloud so
  // dense that every slot touches some point (10,000 uniform points: 0 names) — and fills up to the floor in the same
  // importance order, still never overlapping another label. Covering a point is unavoidable there; naming nothing is
  // worse, and hover still names every point.
  const floor = Math.min(LABEL_FLOOR, pts.filter((p) => p.label).length);
  for (const avoidPoints of [true, false]) {
    if (!avoidPoints && picked.size >= floor) break;
    for (const [i] of order) {
      if (!avoidPoints && picked.size >= floor) break;
      if (picked.has(i)) continue;
      const p = pts[i]!;
      if (!p.label) continue;
      const { px, py } = opts.toPixel(p, i);
      if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
      // Try directly above point first, then below, then beside it (right, left). **Do not force fit through
      // padding** — that shrinks plot area and conflicts with render recipe minimumPlotHeightPx. The side slots exist
      // because labels also avoid points: with only above/below, a point with neighbours directly over and under it
      // could never be named.
      const width = opts.measure(p.label);
      // The caption-named protagonist is always named: it is placed first (no label can be in its way) and does not
      // yield to neighbouring points — a hero point missing its name defeats the caption that points at it.
      const isLead = opts.lead != null && p.label === opts.lead;
      for (const dir of LABEL_SLOTS) {
        const box = labelBox(px, py, width, dir, lineHeight, offset);
        const outside = box.t < opts.area.top || box.b > opts.area.bottom
          || box.l < opts.area.left || box.r > opts.area.right;
        if (outside) continue;
        const hit = placed.some((q) => !(box.r < q.l || box.l > q.r || box.b < q.t || box.t > q.b));
        if (hit) continue;
        const coversPoint = avoidPoints && !isLead && markerGrid.near(box).some(
          (m) => !(m.px === px && m.py === py) && boxHitsCircle(box, m),
        );
        if (coversPoint) continue;
        placed.push(box);
        picked.add(i);
        align.set(i, dir);
        break;
      }
    }
  }
  return { picked, align };
}
