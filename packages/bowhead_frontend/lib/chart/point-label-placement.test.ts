/**
 * Point labels next to bars (116 dogfood, phone 390 dark): a dual-axis line ran inside the bars and its `-24.0%` /
 * `-19.2%` labels were drawn on the bar fill. Geometry below mirrors that chart at 390: three 20px bars, the line's
 * points sitting inside the second and third bar.
 */

import { describe, expect, test } from "vitest";

import {
  candidateOrder,
  placePointLabels,
  pointLabelBox,
  segmentCrossesBox,
  sidewaysLabelIsAmbiguous,
  type LabelBox,
  type PointLabelObstacles,
  type PointLabelRequest,
} from "./point-label-placement";

const BOUNDS: LabelBox = { left: 30, right: 300, top: 0, bottom: 200 };
const bar = (center: number, top: number, bottom = 200): LabelBox => ({
  left: center - 10,
  right: center + 10,
  top,
  bottom,
});
const BARS = [bar(75, 40), bar(165, 90), bar(255, 110)];
/** Bar value labels above each bar top (offset 2, 14.4 tall, 40 wide). */
const BAR_LABELS = BARS.map((b) => ({
  left: (b.left + b.right) / 2 - 20,
  right: (b.left + b.right) / 2 + 20,
  top: b.top - 2 - 14.4,
  bottom: b.top - 2,
}));

const req = (id: string, x: number, y: number, extra: Partial<PointLabelRequest> = {}): PointLabelRequest => ({
  id,
  x,
  y,
  width: 44,
  height: 14.4,
  offset: 4,
  keep: false,
  ...extra,
});

function overlaps(a: LabelBox, b: LabelBox): boolean {
  return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
    && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
}

describe("placePointLabels", () => {
  const obstacles: PointLabelObstacles = {
    fills: BARS,
    labels: BAR_LABELS,
    markers: [
      { x: 165, y: 170, r: 4 },
      { x: 255, y: 150, r: 4 },
    ],
    segments: [{ x1: 165, y1: 170, x2: 255, y2: 150 }],
  };

  test("point inside its own bar with room beside it: the label stays, beside its own bar", () => {
    // Wide gap: a label set 3px off its own column is nearer that column than its point, which is fine; the
    // neighbouring columns are farther than its point (13px).
    const [q2, q3] = placePointLabels([req("q2", 165, 170), req("q3", 255, 150)], obstacles, BOUNDS);
    // Q2: right is clean but crosses the Q2→Q3 line; left is clean and off the line.
    expect(q2).toMatchObject({ align: "left", offset: 13 });
    expect(q2!.box!.right).toBeLessThanOrEqual(BARS[1]!.left - 3 + 1e-9);
    // Q3: right box would end at 255 + 13 + 44 = 312 > 300.
    expect(q3).toMatchObject({ align: "left", offset: 13 });
    for (const p of [q2!, q3!]) {
      for (const other of [...BARS, ...BAR_LABELS]) expect(overlaps(p.box!, other)).toBe(false);
    }
  });

  test("point inside its bar, wide gap to the right only: the label goes right, the roomier side", () => {
    const bars = [bar(40, 60), bar(100, 60), bar(260, 60)];
    const [p] = placePointLabels([req("p", 100, 150)], { fills: bars, labels: [], markers: [] }, BOUNDS);
    expect(p).toMatchObject({ align: "right", offset: 13 });
  });

  test("columns packed tight: every beside spot is pressed against a neighbour, so the yielding label hides", () => {
    // 20px bars every 50px: a 44px label beside its column lands on (or within its point's distance of) the next one.
    const packed = [bar(115, 60), bar(165, 60), bar(215, 60)];
    const [p] = placePointLabels([req("p", 165, 150)], { fills: packed, labels: [], markers: [] }, BOUNDS);
    expect(p!.align).toBeNull();
  });

  test("labels that must stay never sit on a fill, a bar label or a marker: they step beside their bar", () => {
    const placements = placePointLabels(
      [req("q2", 165, 170, { keep: true }), req("q3", 255, 150, { keep: true })],
      obstacles,
      BOUNDS,
    );
    const [q2, q3] = placements;
    expect(q2).toMatchObject({ align: "left", offset: 13 });
    // Q3: right box would end at 255 + 13 + 44 = 312 > 300.
    expect(q3).toMatchObject({ align: "left", offset: 13 });
    for (const p of placements) {
      for (const other of [...BARS, ...BAR_LABELS]) expect(overlaps(p.box!, other)).toBe(false);
      for (const m of obstacles.markers) {
        expect(overlaps(p.box!, { left: m.x - m.r, right: m.x + m.r, top: m.y - m.r, bottom: m.y + m.r })).toBe(false);
      }
    }
    expect(overlaps(q2!.box!, q3!.box!)).toBe(false);
  });

  test("a label skips its own marker but not another point's", () => {
    // Own marker larger than the offset: ignored, so the label stays above.
    const own = placePointLabels([req("p", 120, 100)], { fills: [], labels: [], markers: [{ x: 120, y: 100, r: 10 }] }, BOUNDS);
    expect(own[0]).toMatchObject({ align: "top", offset: 4 });
    // Another point's marker in the "top" spot moves it.
    const other = placePointLabels([req("p", 120, 100)], { fills: [], labels: [], markers: [{ x: 120, y: 88, r: 4 }] }, BOUNDS);
    expect(other[0]!.align).not.toBe("top");
  });

  test("tick text is an obstacle like any other label", () => {
    const tick = { left: 110, right: 130, top: 84, bottom: 96 };
    const [p] = placePointLabels([req("p", 120, 100)], { fills: [], labels: [], markers: [], text: [tick] }, BOUNDS);
    expect(p).toMatchObject({ align: "bottom", offset: 4 });
  });

  test("a point above its bar keeps the builder's default (above, same offset)", () => {
    const [p] = placePointLabels([req("p", 75, 25)], { fills: BARS, labels: BAR_LABELS, markers: [] }, BOUNDS);
    expect(p).toMatchObject({ align: "top", offset: 4 });
  });

  test("a label that would cover a bar value label moves to the next clean side", () => {
    const barLabel = { left: 100, right: 140, top: 60, bottom: 75 };
    const [p] = placePointLabels([req("p", 120, 80)], { fills: [], labels: [barLabel], markers: [] }, BOUNDS);
    expect(overlaps(pointLabelBox(req("p", 120, 80), "top"), barLabel)).toBe(true); // Positive control.
    expect(p).toMatchObject({ align: "bottom", offset: 4 });
  });

  test("no clean position hides a yielding label and keeps the point", () => {
    // Plot only as wide as the bar: every candidate lands on the fill or outside the bounds.
    const narrow = { left: 150, right: 180, top: 0, bottom: 200 };
    const tall = [{ left: 150, right: 180, top: 0, bottom: 200 }];
    const [p] = placePointLabels([req("p", 165, 100)], { fills: tall, labels: [], markers: [] }, narrow);
    expect(p).toMatchObject({ id: "p", align: null, box: null });
  });

  test("a label that must stay (protagonist / no other readback) takes the least-overlapping candidate", () => {
    const narrow = { left: 150, right: 180, top: 0, bottom: 200 };
    const tall = [{ left: 150, right: 180, top: 0, bottom: 200 }];
    const [p] = placePointLabels([req("p", 165, 100, { keep: true })], { fills: tall, labels: [], markers: [] }, narrow);
    expect(p!.align).not.toBeNull();
    expect(p!.box).not.toBeNull();
  });

  test("placed labels are obstacles for later ones (earlier requests win)", () => {
    const a = req("a", 120, 60);
    const b = req("b", 125, 62);
    const [pa, pb] = placePointLabels([a, b], { fills: [], labels: [], markers: [] }, BOUNDS);
    expect(pa).toMatchObject({ align: "top" });
    expect(pb!.align).not.toBe("top");
    if (pb!.box) expect(overlaps(pa!.box!, pb!.box)).toBe(false);
  });

  test("a clean spot off the line wins over a clean spot on it; a line alone never hides a label", () => {
    // Free point (no bars): "top" crosses a steep segment above the point, "bottom" is clear.
    const steep = [{ x1: 120, y1: 100, x2: 122, y2: 40 }];
    const [p] = placePointLabels([req("p", 120, 100)], { fills: [], labels: [], markers: [], segments: steep }, BOUNDS);
    expect(p!.align).toBe("bottom");
    // Every candidate crosses some segment: the first clean candidate is still used.
    const cross = [
      { x1: 80, y1: 70, x2: 160, y2: 130 },
      { x1: 80, y1: 130, x2: 160, y2: 70 },
    ];
    const [q] = placePointLabels([req("q", 120, 100)], { fills: [], labels: [], markers: [], segments: cross }, BOUNDS);
    expect(q!.align).toBe("top");
  });

  test("deterministic: the same input gives the same placements", () => {
    const run = () => placePointLabels([req("q2", 165, 170), req("q3", 255, 150)], obstacles, BOUNDS);
    expect(run()).toEqual(run());
  });
});

describe("sidewaysLabelIsAmbiguous", () => {
  const p = req("p", 100, 100);
  const box = pointLabelBox(p, "right", 4); // 104–148 × 92.8–107.2

  test("within 6px of a bar value label it reads as part of that label", () => {
    expect(sidewaysLabelIsAmbiguous(p, box, [{ left: 153, right: 190, top: 95, bottom: 105 }], [])).toBe(true);
    expect(sidewaysLabelIsAmbiguous(p, box, [{ left: 155, right: 190, top: 95, bottom: 105 }], [])).toBe(false);
  });

  test("nearer another mark than its own point it reads as that mark's", () => {
    // Own point is 4px away; a bar 3px past the label's right edge is nearer.
    expect(sidewaysLabelIsAmbiguous(p, box, [], [{ left: 151, right: 171, top: 50, bottom: 200 }])).toBe(true);
    expect(sidewaysLabelIsAmbiguous(p, box, [], [{ left: 160, right: 180, top: 50, bottom: 200 }])).toBe(false);
  });
});

describe("candidateOrder", () => {
  test("no bar under the point: above first (the builders' default)", () => {
    expect(candidateOrder(req("p", 120, 50), BARS)[0]).toBe("top");
  });

  test("above a bar: above first; inside its bar: beside first, then the vertical side nearer the bar's end", () => {
    expect(candidateOrder(req("p", 165, 80), BARS)).toEqual(["top", "bottom", "right", "left"]);
    expect(candidateOrder(req("p", 165, 100), BARS)).toEqual(["right", "left", "top", "bottom"]);
    expect(candidateOrder(req("p", 165, 190), BARS, ["left", "right"])).toEqual(["left", "right", "bottom", "top"]);
  });

  test("below a bar that grows down (negative value): below first", () => {
    const negative = [{ left: 110, right: 130, top: 100, bottom: 140 }];
    expect(candidateOrder(req("p", 120, 150), negative)).toEqual(["bottom", "top", "right", "left"]);
  });
});

describe("pointLabelBox", () => {
  test("datalabels geometry: the box edge sits `offset` away from the anchor along the align direction", () => {
    const r = req("p", 100, 100);
    expect(pointLabelBox(r, "top")).toEqual({ left: 78, right: 122, top: 81.6, bottom: 96 });
    expect(pointLabelBox(r, "bottom")).toEqual({ left: 78, right: 122, top: 104, bottom: 118.4 });
    expect(pointLabelBox(r, "right", 10)).toEqual({ left: 110, right: 154, top: 92.8, bottom: 107.2 });
    expect(pointLabelBox(r, "left", 10)).toEqual({ left: 46, right: 90, top: 92.8, bottom: 107.2 });
  });
});

describe("segmentCrossesBox", () => {
  const box = { left: 0, right: 10, top: 0, bottom: 10 };
  test("through, inside, outside, and grazing", () => {
    expect(segmentCrossesBox({ x1: -5, y1: 5, x2: 15, y2: 5 }, box)).toBe(true);
    expect(segmentCrossesBox({ x1: 2, y1: 2, x2: 3, y2: 3 }, box)).toBe(true);
    expect(segmentCrossesBox({ x1: -5, y1: -5, x2: -1, y2: 20 }, box)).toBe(false);
    expect(segmentCrossesBox({ x1: -5, y1: 10, x2: 15, y2: 10 }, box)).toBe(false);
  });
});
