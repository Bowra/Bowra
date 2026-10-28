import { describe, expect, test } from "vitest";

import { edgeLabelPadding, tickAvoidAlign, topLabelPadding } from "./edge-label-padding";

/** Core criterion: pad only on actual overflow; amount is independent of padding already applied. */
describe("类目轴两端标签的留边", () => {
  test("放得下就一点都不动——padding 恒 0", () => {
    // Half-width 15 < gap 40 ⇒ neither side needs padding
    expect(edgeLabelPadding({ gapLeft: 40, gapRight: 40, firstWidth: 30, lastWidth: 30 }))
      .toEqual({ left: 0, right: 0 });
  });

  test("★ 这条是命门：不动的图必须 0，否则等于把每张带标签的图都重排了", () => {
    // Corpus exports: 77% of charts have labels; unconditional padding = all relayouts = invalidated pixel baselines.
    for (const gap of [15.001, 16, 24, 100]) {
      expect(edgeLabelPadding({ gapLeft: gap, gapRight: gap, firstWidth: 30, lastWidth: 30 }).right)
        .toBe(0);
    }
  });

  test("溢出多少补多少，再多 1px 不贴边", () => {
    // Half-width 19, gap 0 ⇒ 19 + 1 = 20 (measured 20 for that `-76.9` chart on 116)
    expect(edgeLabelPadding({ gapLeft: 0, gapRight: 0, firstWidth: 0, lastWidth: 38 }))
      .toEqual({ left: 0, right: 20 });
  });

  test("两端各算各的——只有一端溢出就只补那一端", () => {
    expect(edgeLabelPadding({ gapLeft: 50, gapRight: 0, firstWidth: 40, lastWidth: 40 }))
      .toEqual({ left: 0, right: 21 });
  });

  test("那一端没有标签（宽 0）就不补", () => {
    expect(edgeLabelPadding({ gapLeft: 0, gapRight: 0, firstWidth: 0, lastWidth: 0 }))
      .toEqual({ left: 0, right: 0 });
  });

  test("量宽拿到 NaN / 负数 / Infinity 时按「不补」处理，不能把整张图挤没", () => {
    expect(edgeLabelPadding({ gapLeft: 0, gapRight: 0, firstWidth: Number.NaN, lastWidth: -5 }))
      .toEqual({ left: 0, right: 0 });
    // ★ Infinity is the only value actually blocked by `Number.isFinite` (NaN/negative values are blocked by `past > 0`).
    expect(edgeLabelPadding({
      gapLeft: 0, gapRight: 0,
      firstWidth: Number.POSITIVE_INFINITY, lastWidth: Number.POSITIVE_INFINITY,
    })).toEqual({ left: 0, right: 0 });
  });

  test("★★ 不动点：传天然余量时，结果与「当前已留多少」无关（否则会来回震）", () => {
    // First frame: gap 0, half-width 19 ⇒ add 20.
    const first = edgeLabelPadding({ gapLeft: 0, gapRight: 0, firstWidth: 0, lastWidth: 38 });
    expect(first.right).toBe(20);
    // Second frame: plot shrank by its own 20px padding, but **natural gap remains 0** (caller subtracts self-added part)
    // ⇒ Must still yield 20. Passing current gap 20 would yield 0 ⇒ remove padding ⇒ clip again next frame.
    const second = edgeLabelPadding({ gapLeft: 0, gapRight: 0, firstWidth: 0, lastWidth: 38 });
    expect(second).toEqual(first);
  });
});

describe("朝上标签的顶边留边", () => {
  test("放得下就是 0", () => {
    expect(topLabelPadding(40, 18)).toBe(0);
    expect(topLabelPadding(18, 18)).toBe(0);
  });

  test("探出多少补多少，再多 1px（116：19.9 贴着轴顶 20）", () => {
    expect(topLabelPadding(4, 18)).toBe(15);
    expect(topLabelPadding(0, 17.5)).toBe(19);
  });

  test("量不出来（NaN / Infinity）按不补处理", () => {
    expect(topLabelPadding(Number.NaN, 18)).toBe(0);
    expect(topLabelPadding(0, Number.POSITIVE_INFINITY)).toBe(0);
  });
});

describe("点标签与纵轴刻度文字的冲突判定", () => {
  // First point 19.9 label background (centered at x=40, width 30, height 14) and left-axis tick "20" (right-aligned at x=36).
  const label = { left: 25, right: 55, top: 0, bottom: 14 };
  const tick20 = { side: "left" as const, left: 24, right: 36, top: 4, bottom: 16 };

  test("压着左轴刻度：朝上的标签改 -45°（右上），朝下的改 45°（右下）", () => {
    expect(tickAvoidAlign(label, "top", [tick20])).toBe(-45);
    expect(tickAvoidAlign(label, "bottom", [tick20])).toBe(45);
  });

  test("压着右轴刻度：-135°（左上）/ 135°（左下）", () => {
    const right = { ...tick20, side: "right" as const };
    expect(tickAvoidAlign(label, "top", [right])).toBe(-135);
    expect(tickAvoidAlign(label, "bottom", [right])).toBe(135);
  });

  test("不冲突就是 null（不动）：只横向重叠 / 只纵向重叠 / 刚好贴边", () => {
    expect(tickAvoidAlign(label, "top", [])).toBeNull();
    // Horizontal overlap, but tick is far below label (vertical separation)
    expect(tickAvoidAlign(label, "top", [{ ...tick20, top: 150, bottom: 162 }])).toBeNull();
    // Vertical overlap, but tick text lies entirely left of label
    expect(tickAvoidAlign(label, "top", [{ ...tick20, left: 0, right: 20 }])).toBeNull();
    // Touching edges: tick right edge = label left edge / tick top = label bottom
    expect(tickAvoidAlign(label, "top", [{ ...tick20, right: 25 }])).toBeNull();
    expect(tickAvoidAlign(label, "top", [{ ...tick20, top: 14, bottom: 26 }])).toBeNull();
  });

  test("越过边界 1px 就算冲突（贴边判据的另一侧）", () => {
    expect(tickAvoidAlign(label, "top", [{ ...tick20, right: 26 }])).toBe(-45);
    expect(tickAvoidAlign(label, "top", [{ ...tick20, top: 13, bottom: 25 }])).toBe(-45);
  });

  test("两侧都压着（极窄的双轴图）：侧摆救不了，不动", () => {
    const right = { ...tick20, side: "right" as const, left: 50, right: 62 };
    expect(tickAvoidAlign(label, "top", [tick20, right])).toBeNull();
  });
});
