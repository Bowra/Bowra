/**
 * Collision-avoidance criteria for per-point scatter labels (chart-config-ux batch 1).
 *
 * See scatter-labels.ts module comment for the contract: export/touch have no hover,
 * and existing export recipes require tooltip-only information in labels.
 */

import { describe, expect, test } from "vitest";

import { LABEL_FLOOR, labelBox, pickScatterLabels, type LabelPoint } from "./scatter-labels";

/** Fake monospace font: 6px per character, avoiding actual canvas dependency. */
const measure = (s: string) => s.length * 6;
const AREA = { top: 0, bottom: 300, left: 0, right: 600 };

/** Linear canvas mapping: x∈[0,100] → [0,600], y∈[0,100] → [300,0]. */
function linearPixel(p: LabelPoint) {
  return { px: (p.x / 100) * 600, py: 300 - (p.y / 100) * 300 };
}

function overlaps(
  pts: LabelPoint[],
  placement: ReturnType<typeof pickScatterLabels>,
  lineHeight = 12,
  offset = 6,
): number {
  const boxes = [...placement.picked].map((i) => {
    const p = pts[i]!;
    const { px, py } = linearPixel(p);
    const half = measure(p.label) / 2;
    const dir = placement.align.get(i);
    return labelBox(px, py, 2 * half, dir ?? "top", lineHeight, offset);
  });
  let n = 0;
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i]!;
      const b = boxes[j]!;
      if (!(a.r < b.l || a.l > b.r || a.b < b.t || a.t > b.b)) n += 1;
    }
  }
  return n;
}

/** Forty points, deliberately crowded in the middle, matching actual 116 data shape. */
function crowded(): LabelPoint[] {
  const out: LabelPoint[] = [];
  for (let i = 0; i < 40; i += 1) {
    out.push({ x: 30 + (i % 20) * 1.2, y: 20 + ((i * 7) % 30), label: `门店${String(i).padStart(2, "0")}` });
  }
  return out;
}

describe("pickScatterLabels", () => {
  test("★★ 标出来的标签之间零重叠", () => {
    const pts = crowded();
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA });
    expect(overlaps(pts, got)).toBe(0);
  });

  test("★★ 比「只标主角」多得多——这才是这条改动的意义", () => {
    const pts = crowded();
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA });
    expect(got.picked.size).toBeGreaterThan(5);
  });

  test("★ 主角一定被标（图注点名了就不能没有）", () => {
    const pts = crowded();
    const lead = pts[37]!.label;
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA, lead });
    expect(got.picked.has(37)).toBe(true);
  });

  test("★★ 容器变窄时自动少标，而不是糊成一片", () => {
    const pts = crowded();
    const wide = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA });
    const narrowArea = { top: 0, bottom: 150, left: 0, right: 300 };
    const narrow = pickScatterLabels(pts, {
      toPixel: (p) => ({ px: (p.x / 100) * 300, py: 150 - (p.y / 100) * 150 }),
      measure,
      area: narrowArea,
    });
    expect(narrow.picked.size).toBeLessThan(wide.picked.size);
    // Narrow tier also has zero overlaps
    const boxes = [...narrow.picked].map((i) => {
      const p = pts[i]!;
      const px = (p.x / 100) * 300;
      const py = 150 - (p.y / 100) * 150;
      const half = measure(p.label) / 2;
      const dir = narrow.align.get(i);
      return labelBox(px, py, 2 * half, dir ?? "top");
    });
    let hits = 0;
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = i + 1; j < boxes.length; j += 1) {
        const a = boxes[i]!;
        const b = boxes[j]!;
        if (!(a.r < b.l || a.l > b.r || a.b < b.t || a.t > b.b)) hits += 1;
      }
    }
    expect(hits).toBe(0);
  });

  test("★★ 顶到绘图区上边缘的点翻到下方，而不是被丢掉", () => {
    // Prototype found this: 东湖街 at the y-axis top had its label clipped directly above the point.
    const pts: LabelPoint[] = [{ x: 50, y: 100, label: "东湖街" }];
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA });
    expect(got.picked.has(0)).toBe(true);
    expect(got.align.get(0)).toBe("bottom");
  });

  test("★ 上方放得下时就用上方（不要无脑翻）", () => {
    const pts: LabelPoint[] = [{ x: 50, y: 50, label: "翠堤湾" }];
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA });
    expect(got.align.get(0)).toBe("top");
  });

  test("★ 上下都放不下就放弃这个点（宁可不标，不许画到框外）", () => {
    const tiny = { top: 0, bottom: 14, left: 0, right: 600 };
    const pts: LabelPoint[] = [{ x: 50, y: 50, label: "挤死了" }];
    const got = pickScatterLabels(pts, {
      toPixel: () => ({ px: 300, py: 7 }),
      measure,
      area: tiny,
    });
    expect(got.picked.size).toBe(0);
  });

  test("★ 空标签不占位（拿不到名字的点直接跳过）", () => {
    const pts: LabelPoint[] = [
      { x: 10, y: 10, label: "" },
      { x: 50, y: 50, label: "有名字" },
    ];
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA });
    expect(got.picked.has(0)).toBe(false);
    expect(got.picked.has(1)).toBe(true);
  });

  test("★ 坐标算不出来的点跳过，不抛", () => {
    const pts: LabelPoint[] = [{ x: Number.NaN, y: 1, label: "坏点" }];
    const got = pickScatterLabels(pts, {
      toPixel: () => ({ px: Number.NaN, py: Number.NaN }),
      measure,
      area: AREA,
    });
    expect(got.picked.size).toBe(0);
  });
});

// Readability audit (33 scatter charts): labels avoided each other but not the points, so a picked label landed on an
// unlabelled neighbour and hid it under another store's name.
describe("pickScatterLabels never covers a point", () => {
  const R = 5;
  const markersOf = (pts: LabelPoint[]) => pts.map((p) => ({ ...linearPixel(p), r: R }));

  function coveredPoints(pts: LabelPoint[], placement: ReturnType<typeof pickScatterLabels>, lineHeight = 12, offset = 6): number {
    let n = 0;
    for (const i of placement.picked) {
      const { px, py } = linearPixel(pts[i]!);
      const half = measure(pts[i]!.label) / 2;
      const box = labelBox(px, py, 2 * half, placement.align.get(i) ?? "top", lineHeight, offset);
      for (const [j, q] of pts.entries()) {
        if (j === i) continue;
        const m = linearPixel(q);
        const cx = Math.max(box.l, Math.min(m.px, box.r));
        const cy = Math.max(box.t, Math.min(m.py, box.b));
        if ((m.px - cx) ** 2 + (m.py - cy) ** 2 < R * R) n += 1;
      }
    }
    return n;
  }

  test("negative control: without markers the crowded fixture does cover points", () => {
    const pts = crowded();
    expect(coveredPoints(pts, pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA }))).toBeGreaterThan(0);
  });

  test("with every marker passed in, no label box touches another point", () => {
    const pts = crowded();
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA, markers: markersOf(pts) });
    expect(got.picked.size).toBeGreaterThan(0);
    expect(coveredPoints(pts, got)).toBe(0);
    expect(overlaps(pts, got)).toBe(0);
  });

  test("a point's own marker does not block its own label", () => {
    const pts: LabelPoint[] = [{ x: 50, y: 50, label: "孤点" }];
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA, markers: markersOf(pts) });
    expect(got.picked.has(0)).toBe(true);
  });

  test("a label flips below when the space above holds another point", () => {
    // B sits right above A, inside A's label box; A must take the bottom slot.
    const pts: LabelPoint[] = [
      { x: 50, y: 50, label: "AAAA" },
      { x: 50, y: 53, label: "" },
    ];
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA, markers: markersOf(pts) });
    expect(got.align.get(0)).toBe("bottom");
  });
});

// Review follow-ups: the caption-named hero must always be named, side slots, and a floor for saturated clouds.
describe("pickScatterLabels hero, side slots and floor", () => {
  const R = 5;
  const markersOf = (pts: LabelPoint[]) => pts.map((p) => ({ ...linearPixel(p), r: R }));

  // Hero at the centre; points directly above, below, left and right of it occupy every slot.
  function boxedIn(): LabelPoint[] {
    return [
      { x: 50, y: 50, label: "主角门店" },
      { x: 50, y: 53, label: "" },
      { x: 50, y: 47, label: "" },
      { x: 54, y: 50, label: "" },
      { x: 46, y: 50, label: "" },
    ];
  }

  test("the caption-named hero is labelled even when points surround it", () => {
    // Filler keeps the floor pass from rescuing it: the hero must be named on its own merit.
    const filler = Array.from({ length: 20 }, (_, k) => ({ x: 5 + k * 4.5, y: 90, label: `F${k}` }));
    const pts = [...boxedIn(), ...filler];
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA, markers: markersOf(pts), lead: "主角门店" });
    expect(got.picked.has(0)).toBe(true);
  });

  test("negative control: the same point without the caption yields to its neighbours", () => {
    const pts = boxedIn();
    // Floor would rescue it (one labelable point); turn the floor off by adding plenty of other labelable points.
    const filler = Array.from({ length: 20 }, (_, k) => ({ x: 5 + k * 4.5, y: 90, label: `F${k}` }));
    const all = [...pts, ...filler];
    const got = pickScatterLabels(all, { toPixel: linearPixel, measure, area: AREA, markers: markersOf(all) });
    expect(got.picked.has(0)).toBe(false);
  });

  test("a label moves beside its point when above and below hold points", () => {
    const pts: LabelPoint[] = [
      { x: 50, y: 50, label: "AA" },
      { x: 50, y: 54, label: "" }, // 12px above: inside the top slot, clear of the side slots
      { x: 50, y: 46, label: "" },
      // Enough far-away labelable points that the floor pass never runs.
      ...Array.from({ length: 20 }, (_, k) => ({ x: 5 + k * 4.5, y: 90, label: `F${k}` })),
    ];
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA, markers: markersOf(pts) });
    expect(["right", "left"]).toContain(got.align.get(0));
  });

  test("a saturated cloud still keeps a floor of names, none overlapping another label", () => {
    let seed = 7;
    const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    const pts: LabelPoint[] = Array.from({ length: 3000 }, (_, i) => ({ x: rnd() * 100, y: rnd() * 100, label: `S${i}` }));
    const got = pickScatterLabels(pts, { toPixel: linearPixel, measure, area: AREA, markers: markersOf(pts) });
    expect(got.picked.size).toBeGreaterThanOrEqual(LABEL_FLOOR);
    expect(overlaps(pts, got)).toBe(0);
  });
});
