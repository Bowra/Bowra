/**
 * Point-label placement for charts that mix filled marks (bars) with point series (lines).
 *
 * 116 dogfood (phone 390, dark): a dual-axis chart drew its right-axis line inside the bars, and the line's
 * default `align: "top"` labels (`-24.0%`, `-19.2%`) landed on the bar fill — secondary ink on the blue fill, and on
 * top of the markers. datalabels' own `"auto"` only hides a label that overlaps **another label**; it knows nothing
 * about marks.
 *
 * The Chart Embed Spec (D1/D3) puts every data label on the plot background with **no backing plate** and never
 * inside a mark. It has no dual-axis collision rule, so the minimal rule here keeps that contract:
 *  1. a point label must not overlap a filled mark, a fixed (bar value) label, value-axis tick text, another placed
 *     point label or another point's marker;
 *  2. try above/below the point first, on the side away from the bar end it sits against, then beside it (pushed
 *     clear of the bar its point sits on; a point inside its own bar tries beside first, the roomier side first).
 *     A label beside the point must still read as the point's: a sideways spot within 6px of a bar value label, or
 *     nearer a mark of another column (another column's bar, another point) than its own point, counts as taken —
 *     a label pressed against the neighbouring column reads as that column's value. The point's own bar does not
 *     count: the label sits beside the column it belongs to;
 *     Among clean candidates, one that does not cross a drawn line segment wins (text struck through by its own line
 *     reads badly), but crossing a line never hides a label on its own;
 *  3. when no candidate is clean, hide the label (the point stays; the value remains in the tooltip and the exact-value
 *     disclosure). Labels that must stay (the protagonist, or surfaces without another way to read values) take the
 *     candidate with the least overlap instead.
 * Because a placed label never sits on a fill, the on-fill contrast question does not arise and no halo is needed.
 *
 * Pure geometry in canvas pixels (y grows downward), deterministic: same input, same output.
 */

export interface LabelBox {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export type PointLabelAlign = "top" | "bottom" | "right" | "left";

export interface PointLabelRequest {
  /** Caller's key, returned unchanged. */
  id: string;
  /** Anchor (point centre). */
  x: number;
  y: number;
  /** Label box size, including padding. */
  width: number;
  height: number;
  /** Gap between the anchor and the label box (datalabels `offset`). */
  offset: number;
  /** Never hide: take the least-overlapping candidate when none is clean. */
  keep: boolean;
  /** A drawn leader disambiguates sideways text; keep the complete callout inside the plot. */
  leader?: boolean;
}

export interface PointLabelMarker {
  x: number;
  y: number;
  r: number;
}

export interface PointLabelObstacles {
  /** Filled marks a label must not sit on (bar rectangles). */
  fills: LabelBox[];
  /** Labels whose position is fixed (bar value labels). */
  labels: LabelBox[];
  /** Point markers of every visible point series (a request's own marker is skipped). */
  markers: PointLabelMarker[];
  /** Other text a label must not overlap (value-axis tick labels). */
  text?: LabelBox[];
  /** Drawn line segments (soft: avoided when possible, never a reason to hide). */
  segments?: PointLabelSegment[];
}

export interface PointLabelSegment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface PointLabelPlacement {
  id: string;
  /** null = hidden. */
  align: PointLabelAlign | null;
  offset: number;
  box: LabelBox | null;
}

/** Overlaps thinner than this (anti-aliasing, rounding) do not count. */
const TOLERANCE = 0.5;
/** Clearance between a sideways label and the bar its point sits on. */
const SIDE_CLEARANCE = 3;
/** A sideways label closer than this to a bar value label reads as part of it. */
const SIDE_LABEL_PROXIMITY = 6;
/** Cost of an ambiguous sideways spot: positive (so it is never "clean"), below any real overlap area. */
const AMBIGUOUS_SIDE_COST = 1;

/** Shortest distance between two boxes (0 when they touch or overlap). */
function boxGap(a: LabelBox, b: LabelBox): number {
  const dx = Math.max(0, b.left - a.right, a.left - b.right);
  const dy = Math.max(0, b.top - a.bottom, a.top - b.bottom);
  return Math.hypot(dx, dy);
}

/**
 * Would a sideways label be read as belonging to something else? True when it sits within `SIDE_LABEL_PROXIMITY` of a
 * bar value label, or nearer another mark than its own point. `otherMarks` excludes the bar(s) of the point's own
 * column: a label beside the column it belongs to is not misread.
 */
export function sidewaysLabelIsAmbiguous(
  req: PointLabelRequest,
  box: LabelBox,
  fixedLabels: LabelBox[],
  otherMarks: LabelBox[],
): boolean {
  if (fixedLabels.some((label) => boxGap(box, label) < SIDE_LABEL_PROXIMITY)) return true;
  const own = boxGap(box, { left: req.x, right: req.x, top: req.y, bottom: req.y });
  return otherMarks.some((mark) => boxGap(box, mark) < own);
}

export function pointLabelBox(req: PointLabelRequest, align: PointLabelAlign, offset = req.offset): LabelBox {
  const halfW = req.width / 2;
  const halfH = req.height / 2;
  switch (align) {
    case "top":
      return { left: req.x - halfW, right: req.x + halfW, top: req.y - offset - req.height, bottom: req.y - offset };
    case "bottom":
      return { left: req.x - halfW, right: req.x + halfW, top: req.y + offset, bottom: req.y + offset + req.height };
    case "right":
      return { left: req.x + offset, right: req.x + offset + req.width, top: req.y - halfH, bottom: req.y + halfH };
    case "left":
      return { left: req.x - offset - req.width, right: req.x - offset, top: req.y - halfH, bottom: req.y + halfH };
  }
}

function overlapArea(a: LabelBox, b: LabelBox): number {
  const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  if (w <= TOLERANCE || h <= TOLERANCE) return 0;
  return w * h;
}

function markerBox(m: PointLabelMarker): LabelBox {
  return { left: m.x - m.r, right: m.x + m.r, top: m.y - m.r, bottom: m.y + m.r };
}

/** Whether a segment passes through the box interior (Liang–Barsky clip against the box shrunk by the tolerance). */
export function segmentCrossesBox(seg: PointLabelSegment, box: LabelBox): boolean {
  const left = box.left + TOLERANCE;
  const right = box.right - TOLERANCE;
  const top = box.top + TOLERANCE;
  const bottom = box.bottom - TOLERANCE;
  if (left >= right || top >= bottom) return false;
  const dx = seg.x2 - seg.x1;
  const dy = seg.y2 - seg.y1;
  let t0 = 0;
  let t1 = 1;
  const edges: Array<[number, number]> = [
    [-dx, seg.x1 - left],
    [dx, right - seg.x1],
    [-dy, seg.y1 - top],
    [dy, bottom - seg.y1],
  ];
  for (const [p, q] of edges) {
    if (p === 0) {
      if (q < 0) return false;
      continue;
    }
    const t = q / p;
    if (p < 0) {
      if (t > t1) return false;
      if (t > t0) t0 = t;
    } else {
      if (t < t0) return false;
      if (t < t1) t1 = t;
    }
  }
  return t0 <= t1;
}

/** Pixels of the box outside the allowed area: vertical candidates only check top/bottom (the canvas edge padding and
 *  the tick-avoid shift own the horizontal ends), sideways candidates only check left/right. */
function outOfBounds(box: LabelBox, bounds: LabelBox, align: PointLabelAlign): number {
  if (align === "top" || align === "bottom") {
    return Math.max(0, bounds.top - box.top) + Math.max(0, box.bottom - bounds.bottom);
  }
  return Math.max(0, bounds.left - box.left) + Math.max(0, box.right - bounds.right);
}

/** Fills whose horizontal span contains the point: the bar(s) the point sits on or above. */
function fillsUnder(req: PointLabelRequest, fills: LabelBox[]): LabelBox[] {
  return fills.filter((f) => req.x >= f.left - TOLERANCE && req.x <= f.right + TOLERANCE);
}

/**
 * Candidate order: the vertical side away from the bar end the point sits against comes first, then the other side,
 * then beside the point (right before left). Without a bar under the point, above comes first (the builders' default).
 */
export function candidateOrder(
  req: PointLabelRequest,
  fills: LabelBox[],
  sides: ["right", "left"] | ["left", "right"] = ["right", "left"],
): PointLabelAlign[] {
  const under = fillsUnder(req, fills);
  let preferBelow = false;
  let inside = false;
  if (under.length) {
    const top = Math.min(...under.map((f) => f.top));
    const bottom = Math.max(...under.map((f) => f.bottom));
    if (req.y <= top) preferBelow = false;
    else if (req.y >= bottom) preferBelow = true;
    else {
      inside = true;
      preferBelow = req.y - top > bottom - req.y;
    }
  }
  const vertical: PointLabelAlign[] = preferBelow ? ["bottom", "top"] : ["top", "bottom"];
  // Inside its own bar both vertical spots sit on the fill: beside the bar comes first.
  return inside ? [...sides, ...vertical] : [...vertical, ...sides];
}

/** Offset for a sideways label: at least the configured one, and enough to clear the bar the point sits on. */
function sideOffset(req: PointLabelRequest, align: "right" | "left", fills: LabelBox[]): number {
  let offset = req.offset;
  for (const f of fillsUnder(req, fills)) {
    const reach = align === "right" ? f.right - req.x : req.x - f.left;
    offset = Math.max(offset, reach + SIDE_CLEARANCE);
  }
  return offset;
}

/**
 * Place point labels one by one (callers order requests by priority: protagonist first). Each placed label becomes an
 * obstacle for the next. Returns one placement per request, in request order.
 */
export function placePointLabels(
  requests: PointLabelRequest[],
  obstacles: PointLabelObstacles,
  bounds: LabelBox,
): PointLabelPlacement[] {
  const placed: LabelBox[] = [];
  const segments = obstacles.segments ?? [];
  const text = obstacles.text ?? [];
  return requests.map((req) => {
    // The label's own marker is never in its way (the offset clears it); every other point's marker is.
    const markers = obstacles.markers.filter((m) => m.x !== req.x || m.y !== req.y).map(markerBox);
    const own = fillsUnder(req, obstacles.fills);
    const otherMarks = [...obstacles.fills.filter((f) => !own.includes(f)), ...markers];
    // The roomier side first: farther from the neighbouring columns, their value labels and placed labels.
    const room = (align: "right" | "left") => {
      const box = pointLabelBox(req, align, sideOffset(req, align, obstacles.fills));
      const near = [...otherMarks, ...obstacles.labels, ...placed];
      return near.length ? Math.min(...near.map((other) => boxGap(box, other))) : Number.POSITIVE_INFINITY;
    };
    const sides: ["right", "left"] | ["left", "right"] =
      room("left") > room("right") ? ["left", "right"] : ["right", "left"];
    type Candidate = { align: PointLabelAlign; offset: number; box: LabelBox; cost: number };
    let best: Candidate | null = null;
    let firstClean: Candidate | null = null;
    for (const align of candidateOrder(req, obstacles.fills, sides)) {
      const offset = align === "right" || align === "left" ? sideOffset(req, align, obstacles.fills) : req.offset;
      const box = pointLabelBox(req, align, offset);
      const overflow = req.leader ? Math.max(0, bounds.left - box.left) + Math.max(0, box.right - bounds.right) + Math.max(0, bounds.top - box.top) + Math.max(0, box.bottom - bounds.bottom) : outOfBounds(box, bounds, align);
      let cost = overflow * req.height;
      for (const other of [...obstacles.fills, ...obstacles.labels, ...text, ...placed, ...markers]) {
        cost += overlapArea(box, other);
      }
      // An ambiguous sideways spot counts as taken: a yielding label hides rather than read as a bar's value. A label
      // that must stay may still fall back to it — the token cost ranks it ahead of any spot that actually overlaps a
      // bar or another label, since ink on a fill is worse than a label that sits a little close to a bar.
      if (!req.leader && (align === "right" || align === "left")
        && sidewaysLabelIsAmbiguous(req, box, obstacles.labels, otherMarks)) {
        cost += AMBIGUOUS_SIDE_COST;
      }
      if (cost === 0) {
        const candidate = { align, offset, box, cost };
        if (!segments.some((seg) => segmentCrossesBox(seg, box))) {
          best = candidate;
          break;
        }
        firstClean ??= candidate;
        continue;
      }
      if (req.keep && !firstClean && (best === null || cost < best.cost)) best = { align, offset, box, cost };
    }
    if (firstClean && (best === null || best.cost > 0)) best = firstClean;
    if (!best) return { id: req.id, align: null, offset: req.offset, box: null };
    placed.push(best.box);
    return { id: req.id, align: best.align, offset: best.offset, box: best.box };
  });
}
