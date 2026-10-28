/**
 * Batch 2 §3.3: reference lines. Emphasize **honesty**: average only visible series, and qualify averages of truncated views.
 */

import { describe, expect, it, vi } from "vitest";

import { createRefLinePlugin, resolveRefLineValue } from "./ref-line-plugin";
import type { Chart } from "chart.js";

const THEME = {
  ink: "#1A1916",
  inkSoft: "#57544D",
  surface: "#FCFBF8",
} as never;

function fakeChart(datasets: Array<{ data: unknown[]; visible?: boolean }>): Chart {
  return {
    data: { datasets: datasets.map((d) => ({ data: d.data })) },
    isDatasetVisible: (i: number) => datasets[i]?.visible !== false,
  } as unknown as Chart;
}

describe("resolveRefLineValue", () => {
  it("mean：算可见系列的均值", () => {
    expect(resolveRefLineValue(fakeChart([{ data: [1, 2, 3] }]), { kind: "mean" })).toBe(2);
  });

  it("★隐藏的系列不参与均值（否则线画在用户看不见的数据上）", () => {
    const chart = fakeChart([
      { data: [10, 10] },
      { data: [1000, 1000], visible: false },
    ]);
    expect(resolveRefLineValue(chart, { kind: "mean" })).toBe(10);
  });

  it("scatter 形状 {x,y} 取 y", () => {
    const chart = fakeChart([{ data: [{ x: 1, y: 4 }, { x: 2, y: 6 }] }]);
    expect(resolveRefLineValue(chart, { kind: "mean" })).toBe(5);
  });

  it("空数据 / 非数值 → null（不画线，不画 NaN）", () => {
    expect(resolveRefLineValue(fakeChart([{ data: [] }]), { kind: "mean" })).toBeNull();
    expect(resolveRefLineValue(fakeChart([{ data: [null, "x"] }]), { kind: "mean" })).toBeNull();
  });

  it("value：用给定值；缺失/NaN → null", () => {
    const chart = fakeChart([{ data: [1] }]);
    expect(resolveRefLineValue(chart, { kind: "value", value: 42 })).toBe(42);
    expect(resolveRefLineValue(chart, { kind: "value" })).toBeNull();
    expect(resolveRefLineValue(chart, { kind: "value", value: NaN })).toBeNull();
  });
});

describe("createRefLinePlugin 绘制", () => {
  function ctxSpy() {
    return {
      save: vi.fn(), restore: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(),
      lineTo: vi.fn(), stroke: vi.fn(), fillRect: vi.fn(), fillText: vi.fn(),
      setLineDash: vi.fn(), measureText: () => ({ width: 40 }),
      strokeStyle: "", fillStyle: "", lineWidth: 0, font: "", textAlign: "", textBaseline: "",
    };
  }
  function chartWith(ctx: ReturnType<typeof ctxSpy>) {
    return {
      ...fakeChart([{ data: [10, 20, 30] }]),
      ctx,
      chartArea: { left: 0, right: 100, top: 0, bottom: 100 },
      scales: { y: { axis: "y", getPixelForValue: (v: number) => 100 - v } },
    } as unknown as Chart;
  }

  it("画线 + 标签（标签文案走调用方给的口径 formatter）", () => {
    const ctx = ctxSpy();
    const plugin = createRefLinePlugin([{ kind: "mean" }], { theme: THEME, format: (v) => `¥${v}` });
    plugin.afterDatasetsDraw!(chartWith(ctx), {} as never, {} as never, {} as never);
    expect(ctx.stroke).toHaveBeenCalled();
    expect(ctx.fillText).toHaveBeenCalledWith(expect.stringContaining("¥20"), expect.any(Number), expect.any(Number));
  });

  it("★截断视图的均值必须带口径（不假装是全量均值）", () => {
    const ctx = ctxSpy();
    const plugin = createRefLinePlugin(
      [{ kind: "mean", partial: true }],
      { theme: THEME, format: (v) => String(v), partialNote: "（基于前 1,000 行）" },
    );
    plugin.afterDatasetsDraw!(chartWith(ctx), {} as never, {} as never, {} as never);
    expect(ctx.fillText).toHaveBeenCalledWith(expect.stringContaining("基于前 1,000 行"), expect.any(Number), expect.any(Number));
  });

  it("值落在可视区外 → 不画（不画在图框外面）", () => {
    const ctx = ctxSpy();
    const plugin = createRefLinePlugin([{ kind: "value", value: 999 }], { theme: THEME, format: String });
    plugin.afterDatasetsDraw!(chartWith(ctx), {} as never, {} as never, {} as never);
    expect(ctx.stroke).not.toHaveBeenCalled();
  });

  // ─── R4-4 ─────────────────────────────────────────────────────────
  // hbar uses indexAxis="y": y is the **category** axis, x carries values. Previously hardcoded scales.y,
  // so getPixelForValue(mean) treated mean as a row index, with a horizontal line parallel to bars.
  function hbarChart(ctx: ReturnType<typeof ctxSpy>) {
    return {
      ...fakeChart([{ data: [10, 20, 30] }]),
      ctx,
      options: { indexAxis: "y" },
      chartArea: { left: 0, right: 100, top: 0, bottom: 100 },
      scales: {
        x: { id: "x", axis: "x", getPixelForValue: (v: number) => v },
        // Category-axis mapping must distinguish misuse
        y: { id: "y", axis: "y", getPixelForValue: () => 999 },
      },
    } as unknown as Chart;
  }

  it("★R4-4 hbar 取值轴（x）而不是类目轴（y），且线竖着画", () => {
    const ctx = ctxSpy();
    const plugin = createRefLinePlugin([{ kind: "mean" }], { theme: THEME, format: String });
    plugin.afterDatasetsDraw!(hbarChart(ctx), {} as never, {} as never, {} as never);
    expect(ctx.stroke).toHaveBeenCalled();
    // Mean 20 maps to 20 on x; vertical line from (20, top) to (20, bottom)
    expect(ctx.moveTo).toHaveBeenCalledWith(20, 0);
    expect(ctx.lineTo).toHaveBeenCalledWith(20, 100);
  });

  it("★R5-6 hbar 上均值贴近左边时，标签翻到线右侧（不画到图外/盖住类目刻度）", () => {
    const ctx = ctxSpy();
    // Long-tail range puts mean far left (x=8); a 40-wide right-aligned label would extend to -38
    const chart = {
      ...fakeChart([{ data: [8, 8, 8] }]),
      ctx,
      options: { indexAxis: "y" },
      chartArea: { left: 0, right: 100, top: 0, bottom: 100 },
      scales: { x: { id: "x", axis: "x", getPixelForValue: (v: number) => v } },
    } as unknown as Chart;
    createRefLinePlugin([{ kind: "mean" }], { theme: THEME, format: String })
      .afterDatasetsDraw!(chart, {} as never, {} as never, {} as never);
    const [boxLeft] = ctx.fillRect.mock.calls[0]!;
    expect(boxLeft).toBeGreaterThanOrEqual(0);
  });

  it("★R4-4b 双轴：均值只算挂在同一条值轴上的系列（不把 亿 和 % 加一起）", () => {
    const chart = {
      data: {
        datasets: [
          { data: [10, 20], yAxisID: "y" },
          { data: [1000, 2000], yAxisID: "y1" },
        ],
      },
      isDatasetVisible: () => true,
      options: {},
      scales: {
        y: { id: "y", axis: "y", getPixelForValue: (v: number) => v },
        y1: { id: "y1", axis: "y", getPixelForValue: (v: number) => v },
      },
    } as unknown as Chart;
    expect(resolveRefLineValue(chart, { kind: "mean" })).toBe(15);
  });

  it("★R4-5 每条线可带自己的口径文案（拉取截断 vs 视图截断不是同一句）", () => {
    const ctx = ctxSpy();
    const plugin = createRefLinePlugin(
      [{ kind: "mean", partial: true, partialNote: "（基于当前视图）" }],
      { theme: THEME, format: String, partialNote: "（基于已加载部分）" },
    );
    plugin.afterDatasetsDraw!(chartWith(ctx), {} as never, {} as never, {} as never);
    expect(ctx.fillText).toHaveBeenCalledWith(
      expect.stringContaining("基于当前视图"), expect.any(Number), expect.any(Number),
    );
  });

  it("没有参考线 → 完全不动 canvas", () => {
    const ctx = ctxSpy();
    createRefLinePlugin([], { theme: THEME, format: String }).afterDatasetsDraw!(
      chartWith(ctx), {} as never, {} as never, {} as never,
    );
    expect(ctx.save).not.toHaveBeenCalled();
  });
});
