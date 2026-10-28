/**
 * Series visibility consolidation — criteria + application to Chart.js configuration.
 *
 * ★ Why new coverage is required: **canvas legend clicks previously had zero coverage across the repository**. That path uses Chart.js
 *   default handler (we never overrode `legend.onClick`), so passing tests after changes
 *   proves nothing here. Pin three consolidated behaviors:
 *     ① Legend clicks change **our state** (callback receives series key, not datasetIndex);
 *     ② Hiding sets dataset.hidden (**without deleting columns** ⇒ no chart-kind downgrade);
 *     ③ Export surface always renders every series.
 */

import { describe, expect, it, vi } from "vitest";

import { buildChartJSConfig } from "./build-config";
import {
  effectiveHiddenSeries,
  numericSeriesKeys,
  seriesKeysOfDatasets,
  toggleSeriesKey,
} from "./series-visibility";
import type { ChartSpec } from "./types";

const STUB_THEME = {
  ink: "#1A1916", inkSoft: "#57544D",
  primary: "#2C5F2D",
  palette: ["#2C5F2D", "#97BC62", "#F4A261", "#E76F51"],
  grid: "#E5E2DA",
  surface: "#FCFBF8",
};

/** 1 dimension + 2 numeric columns: legend items = series (exact class affected by consolidation). */
function twoSeries(kind: ChartSpec["kind"] = "line"): ChartSpec {
  return {
    kind,
    title: "门店营业额与退款率",
    columns: [
      { key: "m", header: "月份", type: "categorical" },
      { key: "amt", header: "营业额", type: "numeric" },
      { key: "rate", header: "退款率", type: "numeric" },
    ],
    rows: [
      { m: "1月", amt: 100, rate: 0.29 },
      { m: "2月", amt: 90, rate: 0.13 },
    ],
    row_count: 2,
  };
}

const legendOf = (cfg: ReturnType<typeof buildChartJSConfig>) =>
  (cfg.options as { plugins?: { legend?: Record<string, unknown> } }).plugins?.legend ?? {};

const hiddenFlags = (cfg: ReturnType<typeof buildChartJSConfig>) =>
  cfg.data.datasets.map((ds) => (ds as { hidden?: boolean }).hidden);

/** Simulate one legend click dispatched by Chart.js. */
function clickLegend(cfg: ReturnType<typeof buildChartJSConfig>, datasetIndex: number) {
  const onClick = legendOf(cfg).onClick as
    | ((e: unknown, item: { datasetIndex: number }, legend: unknown) => void)
    | undefined;
  if (!onClick) throw new Error("legend.onClick 没有被覆写");
  onClick({ type: "click" }, { datasetIndex }, {});
}

describe("toggleSeriesKey（点一下 = 隐藏 ⇄ 显示）", () => {
  it("没藏过就藏起来，再点一次放回来", () => {
    expect(toggleSeriesKey([], "rate")).toEqual(["rate"]);
    expect(toggleSeriesKey(["rate"], "rate")).toEqual([]);
  });

  it("★语义是 toggle，不是 solo —— 点第二条不会把第一条放回来", () => {
    // Customer feedback "想只看某一条线要点很多次" is a next-round product decision; this round only unifies implementation.
    expect(toggleSeriesKey(["amt"], "rate")).toEqual(["amt", "rate"]);
  });

  it("undefined 起步也能用（view.hiddenSeries 初始就是 undefined）", () => {
    expect(toggleSeriesKey(undefined, "amt")).toEqual(["amt"]);
  });
});

describe("effectiveHiddenSeries（两个入口共用的那一条判据）", () => {
  it("不认识的 key 忽略（换了 spec 之后旧 key 不该继续压着某一列）", () => {
    expect([...effectiveHiddenSeries(["amt", "rate"], ["gone"])]).toEqual([]);
  });

  it("★全隐藏时 fail-open：列序第一条留着（review R3③ 的规则，现在两侧同用）", () => {
    expect([...effectiveHiddenSeries(["amt", "rate"], ["amt", "rate"])]).toEqual(["rate"]);
  });

  it("没藏满就照藏", () => {
    expect([...effectiveHiddenSeries(["amt", "rate"], ["rate"])]).toEqual(["rate"]);
  });

  it("numericSeriesKeys 只认数值列", () => {
    expect(numericSeriesKeys(twoSeries().columns)).toEqual(["amt", "rate"]);
  });
});

describe("seriesKeysOfDatasets（哪些图的图例才是「系列图例」）", () => {
  it("逐 dataset 一个不同的 _yCol → 认", () => {
    expect(seriesKeysOfDatasets([{ _yCol: { key: "amt" } }, { _yCol: { key: "rate" } }]))
      .toEqual(["amt", "rate"]);
  });

  it("★同一个 _yCol 重复（散点分组 / 堆叠柱 pivot：图例项是组名不是系列）→ 不认", () => {
    expect(seriesKeysOfDatasets([{ _yCol: { key: "amt" } }, { _yCol: { key: "amt" } }])).toBeNull();
  });

  it("没有 _yCol（饼图那条）→ 不认", () => {
    expect(seriesKeysOfDatasets([{}, {}])).toBeNull();
  });

  it("单系列 → 不认（藏掉它就是空图，交给库默认）", () => {
    expect(seriesKeysOfDatasets([{ _yCol: { key: "amt" } }])).toBeNull();
  });
});

describe("★图例点击落到我们的状态上（此前零覆盖）", () => {
  it.each(["line", "bar", "dual_axis", "combo"] as const)(
    "%s：点第 2 个图例项，回调收到的是那一列的 key",
    (kind) => {
      const onToggleSeries = vi.fn();
      const cfg = buildChartJSConfig(twoSeries(kind), STUB_THEME as never, { onToggleSeries });
      clickLegend(cfg, 1);
      expect(onToggleSeries).toHaveBeenCalledWith("rate");
      clickLegend(cfg, 0);
      expect(onToggleSeries).toHaveBeenLastCalledWith("amt");
    },
  );

  it("不传 onToggleSeries 就不覆写（保持 Chart.js 默认处理器）", () => {
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, {});
    expect(legendOf(cfg).onClick).toBeUndefined();
  });

  it("datasetIndex 对不上时什么也不做（不拿 undefined 去写状态）", () => {
    const onToggleSeries = vi.fn();
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, { onToggleSeries });
    clickLegend(cfg, 99);
    expect(onToggleSeries).not.toHaveBeenCalled();
  });

  it("★长表堆叠柱：pivot 之后每个切片就是一条真系列（实测，不是推的）", () => {
    // `pivotLongSpec` in `normalizeChartForRenderingV1` converts branch × channel × amount
    // into one column per channel, then builder creates datasets by column ⇒ each dataset has a **different**
    // `_yCol`; legend items truly are series. These specs originally have only 1 numeric column, so controls cannot show chips;
    // legend is their only entry point.
    const pivot: ChartSpec = {
      kind: "stacked_bar",
      title: "分行 × 渠道",
      columns: [
        { key: "br", header: "分行", type: "categorical" },
        { key: "ch", header: "渠道", type: "categorical" },
        { key: "amt", header: "金额", type: "numeric" },
      ],
      rows: [
        { br: "北京", ch: "线上", amt: 10 },
        { br: "北京", ch: "线下", amt: 5 },
        { br: "上海", ch: "线上", amt: 8 },
        { br: "上海", ch: "线下", amt: 6 },
      ],
      row_count: 4,
    };
    const onToggleSeries = vi.fn();
    const cfg = buildChartJSConfig(pivot, STUB_THEME as never, { onToggleSeries });
    expect(cfg.data.datasets.length).toBeGreaterThan(1);
    const secondKey = (cfg.data.datasets[1] as { _yCol?: { key: string } })._yCol?.key;
    expect(secondKey).toBeTruthy();

    clickLegend(cfg, 1);
    expect(onToggleSeries).toHaveBeenCalledWith(secondKey);

    // Hiding this slice affects only it; other slices remain unchanged
    const hiddenCfg = buildChartJSConfig(pivot, STUB_THEME as never, { hiddenSeries: [secondKey!] });
    expect(hiddenFlags(hiddenCfg)).toEqual(cfg.data.datasets.map((_, i) => i === 1));
  });
});

describe("★隐藏落成 dataset.hidden（不删列 ⇒ 图种不变）", () => {
  it("藏掉的那条 hidden=true，其余 false", () => {
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, { hiddenSeries: ["rate"] });
    expect(hiddenFlags(cfg)).toEqual([false, true]);
  });

  it("★★取消隐藏必须写显式 false，不能省略键", () => {
    // react-chartjs-2 updates datasets with Object.assign(current, next) (overwrite only, no key deletion).
    // Omitting key ⇒ previous hidden:true remains on instance forever ⇒ clicking again has no effect.
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, { hiddenSeries: [] });
    expect(hiddenFlags(cfg)).toEqual([false, false]);
    for (const ds of cfg.data.datasets) expect(ds).toHaveProperty("hidden");
  });

  it("★列一根没少（dual_axis 藏一条仍是 2 个 dataset、仍有右轴）", () => {
    const cfg = buildChartJSConfig(twoSeries("dual_axis"), STUB_THEME as never, {
      hiddenSeries: ["rate"],
    });
    expect(cfg.data.datasets).toHaveLength(2);
    expect((cfg.options as { scales?: Record<string, unknown> }).scales?.y1).toBeDefined();
  });

  it("全藏时 fail-open，第一条仍然可见", () => {
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, {
      hiddenSeries: ["amt", "rate"],
    });
    expect(hiddenFlags(cfg)).toEqual([false, true]);
  });

  it("两个入口都没接的老调用：config 上根本不长 hidden 这个键", () => {
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, {});
    for (const ds of cfg.data.datasets) expect(ds).not.toHaveProperty("hidden");
  });
});

describe("图例项不是「系列」的图：一律不碰", () => {
  it("饼图：不覆写 onClick，也不打 hidden（图例项是类目，库默认才是对的）", () => {
    const pie: ChartSpec = {
      kind: "pie",
      title: "渠道占比",
      columns: [
        { key: "ch", header: "渠道", type: "categorical" },
        { key: "amt", header: "营业额", type: "numeric" },
      ],
      rows: [{ ch: "天猫", amt: 60 }, { ch: "京东", amt: 40 }],
      row_count: 2,
    };
    const cfg = buildChartJSConfig(pie, STUB_THEME as never, {
      onToggleSeries: vi.fn(),
      hiddenSeries: ["amt"],
    });
    expect(legendOf(cfg).onClick).toBeUndefined();
    expect(hiddenFlags(cfg)).toEqual([undefined]);
  });

  it("★散点分组：多个 dataset 带**同一个** _yCol（图例项是组名）→ 不碰", () => {
    const scatter: ChartSpec = {
      kind: "scatter",
      title: "面积 vs 营业额",
      columns: [
        { key: "grp", header: "业态", type: "categorical" },
        { key: "area", header: "面积", type: "numeric" },
        { key: "amt", header: "营业额", type: "numeric" },
      ],
      rows: [
        { grp: "购物中心", area: 100, amt: 10 },
        { grp: "购物中心", area: 120, amt: 12 },
        { grp: "街边店", area: 60, amt: 6 },
        { grp: "街边店", area: 80, amt: 7 },
      ],
      row_count: 4,
    };
    const cfg = buildChartJSConfig(scatter, STUB_THEME as never, {
      onToggleSeries: vi.fn(),
      hiddenSeries: ["amt"],
    });
    expect(cfg.data.datasets.length).toBeGreaterThan(1);
    expect(legendOf(cfg).onClick).toBeUndefined();
    expect(hiddenFlags(cfg).every((flag) => flag === undefined)).toBe(true);
  });
});

describe("export surface: the exporting user's hidden series, never a legend handler", () => {
  // User decision 2026-10-01 (option b): an export follows the exporting user's legend-hidden series, which reach the
  // pure function as `hiddenSeries` from the export payload. It stays static: no legend click handler.
  it("surface=export applies hiddenSeries but never takes over the legend", () => {
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, {
      surface: "export",
      hiddenSeries: ["rate"],
      onToggleSeries: vi.fn(),
    });
    expect(hiddenFlags(cfg)).toEqual([false, true]);
    expect(legendOf(cfg).onClick).toBeUndefined();
    expect(cfg.data.datasets).toHaveLength(2);
  });

  it("surface=export without hidden series renders every series untouched", () => {
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, { surface: "export" });
    expect(hiddenFlags(cfg).every((flag) => flag === undefined)).toBe(true);
  });

  it("正对照：同一份 spec 在画布面上确实会被藏（否则上一条可能只是 build 没接线）", () => {
    const cfg = buildChartJSConfig(twoSeries(), STUB_THEME as never, {
      surface: "canvas",
      hiddenSeries: ["rate"],
    });
    expect(hiddenFlags(cfg)).toEqual([false, true]);
  });
});
