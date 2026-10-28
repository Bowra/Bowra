/**
 * V2.9 B1 · unit tests for buildCombo (bars+lines on one axis) + buildDualAxis (two axes).
 */

import { BarController, Interaction, PointElement, type Chart, type ChartEvent, type ChartType, type TooltipItem, type TooltipModel } from "chart.js";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { buildChartJSConfig, labelLineHeight, POINT_LABEL_KEEP_ALL_KEY } from "./build-config";
import { labelBox } from "./scatter-labels";
import { formatPercentCell, resolvePercentScale } from "./format";
import type { ChartRenderContext } from "./render-context";
import type { ChartColumn, ChartSpec } from "./types";

// ⚠ R7: this substitute uses a **six-digit** surface hex, but production CSS compresses `--c-surface` to
// **`#fff` (three digits)**. The substitute accepts a broader shape than actual input, so the 2026-08-14 black-label-background
// production incident cannot be detected here (see final ★R7 case and lib/chart/color.test.ts).
// Think before changing this value: relaxing input shape excludes an entire class of production defects from testing.
const STUB_THEME = {
  ink: "#1A1916", inkSoft: "#57544D",
  primary: "#2C5F2D",
  palette: ["#2C5F2D", "#97BC62", "#F4A261", "#E76F51"],
  grid: "#E5E2DA",
  surface: "#FCFBF8",
};

/** Is this cell's label enabled? Since batch 2, datalabels `display` has three states:
 *  `false` off / `true` on and never hidden (protagonist) / `"auto"` on but hidden upon overlap.
 *  Most assertions only care about enabled/disabled; hardcoding `toBe(true)` would pin three states to two.
 *  The assertion distinguishing protagonist from others lives at file end (★three states). */
const shown = (d: unknown) => d === true || d === "auto";

/** Per-chart plugins attached to config (by id, not count). */
const pluginIds = (cfg: ReturnType<typeof buildChartJSConfig>) =>
  (cfg.plugins ?? []).map((p) => p.id);

/** Production-shaped theme: CSS compresses `--c-surface` to three digits. */
const PROD_SHAPE_THEME = { ...STUB_THEME, surface: "#fff" };

describe("source values and readable display magnitudes", () => {
  let originalLocale: string;
  beforeEach(() => { originalLocale = document.documentElement.lang; document.documentElement.lang = "zh-CN"; });
  afterEach(() => { document.documentElement.lang = originalLocale; });

  const losses: ChartSpec = {
    kind: "hbar", title: "Prepared losses", row_count: 3, show_data_labels: true,
    columns: [
      { key: "name", header: "Category", type: "categorical" },
      { key: "value", header: "Loss_亿元", type: "numeric", unit: "亿元" },
    ],
    rows: [{ name: "A", value: 0.0009 }, { name: "B", value: 0.0000078 }, { name: "C", value: -0.00000025 }],
  };

  test.each(["bar", "hbar", "line", "area_stacked", "combo", "dual_axis"] as const)("%s separates axis, label and hover units without changing coordinates", (kind) => {
    const spec: ChartSpec = { ...losses, kind,
      ...(kind === "combo" || kind === "dual_axis" ? {
        columns: [...losses.columns, { key: "other", header: "Other", type: "numeric", unit: kind === "dual_axis" ? "%" : "亿元", ...(kind === "dual_axis" ? { format: "percent" as const, percent_scale: "fraction" as const } : {}) }],
        rows: losses.rows!.map((row) => ({ ...row, other: kind === "dual_axis" ? 0.123456 : 0.0008 })),
      } : {}),
    };
    const before = JSON.stringify(spec);
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    const axis = cfg.options.scales![kind === "hbar" ? "x" : "y"] as any;
    expect(axis.title.text).toContain("万元");
    expect(axis.ticks.callback(0.0009)).toBe("9");
    const dataset = cfg.data.datasets[0] as any;
    expect(dataset.data[0]).toBe(0.0009);
    expect(dataset.datalabels.formatter(0.0009)).toBe("9万元");
    expect(dataset.datalabels.formatter(0.0000078)).toBe("780元");
    const hover = cfg.options.plugins!.tooltip!.callbacks!.label!;
    const text = hover.call({} as TooltipModel<ChartType>, {
      dataset, dataIndex: 0, parsed: { x: 0.0009, y: 0.0009 }, chart: { options: cfg.options },
    } as unknown as TooltipItem<ChartType>);
    expect(text).toBe("Loss: 90,000元");
    if (kind === "dual_axis") {
      const right = cfg.options.scales!.y1 as any;
      expect(right.title.text).toContain("%");
      expect(right.ticks.callback(0.1)).toBe("10");
    }
    expect(JSON.stringify(spec)).toBe(before);
  });

  test("scatter axes use independent currency magnitudes", () => {
    const cfg = buildChartJSConfig({ kind: "scatter", title: "Costs", row_count: 2,
      columns: [{ key: "x", header: "Revenue", type: "numeric", unit: "元" }, { key: "y", header: "Cost", type: "numeric", unit: "元" }],
      rows: [{ x: 1e8, y: 1000 }, { x: 2e8, y: 2000 }],
    }, STUB_THEME as any);
    expect((cfg.options.scales!.x as any).title.text).toBe("Revenue（万元）");
    expect((cfg.options.scales!.y as any).title.text).toBe("Cost（元）");
    expect((cfg.options.scales!.x as any).ticks.callback(1e8)).toBe("10,000");
  });

  test("shared domains keep the axis magnitude stable across prepared frames", () => {
    const axisDomains = { encoding: "hbar" as const, domains: { x: [0, 3] as [number, number] } };
    for (const values of [[0.0009, 0.0000078], [1, 2]]) {
      const cfg = buildChartJSConfig({ ...losses, rows: values.map((value, i) => ({ name: String(i), value })) }, STUB_THEME as any, { axisDomains });
      const axis = cfg.options.scales!.x as any;
      expect(axis.title.text).toBe("亿元");
      expect(axis.ticks.callback(1)).toBe("1");
      expect([axis.min, axis.max]).toEqual([0, 3]);
      expect((cfg.data.datasets[0] as any).datalabels.formatter(0.0000078)).toBe("780元");
    }
  });

  test("stacked area uses separate positive and negative totals to select magnitude", () => {
    const cfg = buildChartJSConfig({ ...losses, kind: "area_stacked",
      columns: [losses.columns[0]!, ...["a", "b", "c"].map((key) => ({ key, header: key, type: "numeric" as const, unit: "元" }))],
      rows: [{ name: "A", a: 6000, b: 5000, c: -9000 }],
    }, STUB_THEME as any);
    expect((cfg.options.scales!.y as any).title.text).toBe("万元");
    expect((cfg.options.scales!.y as any).ticks.callback(10000)).toBe("1");
  });

  test("a domain for another encoding cannot override the current axis magnitude", () => {
    const cfg = buildChartJSConfig(losses, STUB_THEME as any, { axisDomains: { encoding: "scatter", domains: { x: [0, 3] } } });
    expect((cfg.options.scales!.x as any).title.text).toBe("万元");
    expect(cfg.options.scales!.x!.max).toBeUndefined();
  });

  test("native ticks finer than the observed differences remain distinct on one axis", () => {
    const cfg = buildChartJSConfig({ ...losses, columns: [losses.columns[0]!, { ...losses.columns[1]!, unit: "元" }],
      rows: [{ name: "A", value: 1e9 }, { name: "B", value: 1e9 + 1 }],
    }, STUB_THEME as any);
    const axis = cfg.options.scales!.x as any;
    const ticks = Array.from({ length: 6 }, (_, i) => ({ value: 1e9 + i / 5 }));
    const labels = ticks.map((tick, index) => axis.ticks.callback(tick.value, index, ticks));
    expect(new Set(labels).size).toBe(6);
    expect(axis.title.text).toBe("亿元");
  });
});

describe("native mark observation readouts", () => {
  const observed: ChartSpec = {
    kind: "heatmap", title: "Observed values", row_count: 1,
    columns: [
      { key: "from", header: "Origin", type: "categorical" },
      { key: "to", header: "Destination", type: "categorical" },
      { key: "amount", header: "Amount", type: "numeric", unit: "USD" },
    ],
    rows: [{ from: "A", to: "B", amount: 1234.56789 }],
  };

  test.each(["heatmap", "sankey", "waterfall"] as const)("%s retains the measure unit with ordinary money precision", (kind) => {
    const cfg = buildChartJSConfig({ ...observed, kind }, WP2_THEME);
    const callback = cfg.options.plugins!.tooltip!.callbacks!.label!;
    const result = callback.call({} as TooltipModel<ChartType>, {
      dataset: cfg.data.datasets[0], dataIndex: 0, raw: cfg.data.datasets[0].data[0],
    } as unknown as TooltipItem<ChartType>);
    expect(result).toContain("USD 1,234.57");
  });

  test("scatter reads each axis independently with its own unit", () => {
    const cfg = buildChartJSConfig({ ...observed, kind: "scatter",
      columns: [...observed.columns, { key: "people", header: "People", type: "numeric", unit: "people" }],
      rows: [{ from: "A", to: "B", amount: 1234.56789, people: 17 }],
    }, WP2_THEME);
    const callback = cfg.options.plugins!.tooltip!.callbacks!.label!;
    const result = callback.call({} as TooltipModel<ChartType>, {
      dataset: cfg.data.datasets[0], dataIndex: 0, parsed: { x: 1234.56789, y: 17 }, chart: { options: cfg.options },
    } as unknown as TooltipItem<ChartType>);
    expect(result).toContain("Amount USD 1,234.57");
    expect(result).toContain("People 17people");
  });

  test("heatmap carries measure units and percentage shape into the existing color legend", () => {
    expect(buildChartJSConfig(observed, WP2_THEME).heatmapLegend).toMatchObject({ unit: "USD", min: 1234.56789, max: 1234.56789 });
    const spec: ChartSpec = { ...observed, columns: observed.columns.map((column) => column.key === "amount" ? { ...column, unit: "%", format: "percent", percent_scale: "fraction" } : column) };
    expect(buildChartJSConfig(spec, WP2_THEME).heatmapLegend).toMatchObject({ percent: "fraction" });
  });

  test.each(["bar", "hbar", "combo", "dual_axis"] as const)("%s keeps missing observations distinct from real zero", (kind) => {
    const spec: ChartSpec = { ...observed, kind,
      columns: [...observed.columns, { key: "second", header: "Second", type: "numeric", unit: "USD" }],
      rows: [{ from: "A", to: "B", amount: null, second: 0 }, { from: "C", to: "D", amount: 0, second: null }],
      row_count: 2,
    };
    const cfg = buildChartJSConfig(spec, WP2_THEME);
    expect(cfg.data.datasets[0].data).toEqual([null, 0]);
    expect(cfg.data.datasets[1].data).toEqual([0, null]);
  });

  test("heatmap omits absent cells, preserves real zero and excludes missing data from its color range", () => {
    const spec: ChartSpec = { ...observed, row_count: 3,
      rows: [{ from: "A", to: "B", amount: null }, { from: "C", to: "D", amount: 0 }, { from: "E", to: "F", amount: 10 }],
    };
    const cfg = buildChartJSConfig(spec, WP2_THEME);
    expect(cfg.data.datasets[0].data).toEqual([{ x: "C", y: "D", v: 0 }, { x: "E", y: "F", v: 10 }]);
    expect(cfg.heatmapLegend).toMatchObject({ min: 0, max: 10 });
    expect(buildChartJSConfig({ ...spec, rows: [{ from: "A", to: "B", amount: null }] }, WP2_THEME).heatmapLegend).toBeUndefined();
  });
});

const PRESET_THEME = {
  ...STUB_THEME,
  accessibility: { contrast: "standard", colorVision: "standard" },
  gridSoft: "#EEEAE2",
  lineWidth: 2,
  pointRadius: 3,
  barRadius: 4,
  gridDensity: "quiet",
};

const COMPACT_CONTEXT: ChartRenderContext = {
  placement: "chat",
  bounds: { widthPx: 344, heightPx: 320, devicePixelRatio: 2 },
  input: { hover: false, pointer: "coarse", keyboard: true },
  output: { medium: "screen", static: false },
  mode: "light",
  locale: "zh-CN",
  reducedMotion: false,
  forcedColors: false,
  exactDataAlternative: { kind: "inspect-table" },
};

function _spec(kind: ChartSpec["kind"], extras: Partial<ChartSpec> = {}): ChartSpec {
  return {
    kind, title: "test",
    columns: [
      { key: "quarter", header: "Quarter", type: "categorical" },
      { key: "target", header: "Target", type: "numeric" },
      { key: "actual", header: "Actual", type: "numeric" },
      { key: "forecast", header: "Forecast", type: "numeric" },
    ],
    rows: [
      { quarter: "24Q1", target: 3.7, actual: 4.0, forecast: 0 },
      { quarter: "24Q2", target: 3.9, actual: 3.7, forecast: 0 },
      { quarter: "25Q3", target: 5.4, actual: 5.4, forecast: 0 },
      { quarter: "25Q4", target: 5.5, actual: 0, forecast: 5.5 },
    ],
    row_count: 4,
    ...extras,
  };
}

describe("buildCombo (V2.9 B1)", () => {
  test("生成 1 bar + N-1 line, 共 N datasets", () => {
    const cfg = buildChartJSConfig(_spec("combo"), STUB_THEME as any);
    expect(cfg.type).toBe("bar");  // Main chart.js type.
    expect(cfg.data.datasets).toHaveLength(3);
    expect(cfg.data.datasets[0].type).toBe("bar");
    expect(cfg.data.datasets[0].label).toBe("Target");
    expect(cfg.data.datasets[1].type).toBe("line");
    expect(cfg.data.datasets[1].label).toBe("Actual");
    expect(cfg.data.datasets[2].type).toBe("line");
    expect(cfg.data.datasets[2].label).toBe("Forecast");
  });

  test("所有 dataset 共享同 y 轴 (无 yAxisID)", () => {
    const cfg = buildChartJSConfig(_spec("combo"), STUB_THEME as any);
    for (const ds of cfg.data.datasets) {
      expect((ds as any).yAxisID).toBeUndefined();
    }
    // scales should not have y1.
    expect((cfg.options as any).scales.y).toBeDefined();
    expect((cfg.options as any).scales.y1).toBeUndefined();
  });

  test("缺少 ≥2 numeric 抛 error", () => {
    const broken: ChartSpec = {
      kind: "combo", title: "x",
      columns: [
        { key: "q", header: "Q", type: "categorical" },
        { key: "v", header: "V", type: "numeric" },
      ],
      rows: [{ q: "1", v: 1 }],
      row_count: 1,
    };
    expect(() => buildChartJSConfig(broken, STUB_THEME as any)).toThrow(
      /≥2 numeric/,
    );
  });

  test("labels 来自 categorical 列", () => {
    const cfg = buildChartJSConfig(_spec("combo"), STUB_THEME as any);
    expect(cfg.data.labels).toEqual(["24Q1", "24Q2", "25Q3", "25Q4"]);
  });

  test("bar order=2 (下层) line order=1 (上层) 防被 bar 遮", () => {
    const cfg = buildChartJSConfig(_spec("combo"), STUB_THEME as any);
    const bar = cfg.data.datasets[0] as any;
    const line = cfg.data.datasets[1] as any;
    expect(bar.order).toBe(2);
    expect(line.order).toBe(1);
  });
});

describe("buildDualAxis (V2.9 B1: 区分 combo)", () => {
  test("dual_axis 跟 combo 区别: 第 2 系列 yAxisID=y1 (右轴)", () => {
    const cfg = buildChartJSConfig(_spec("dual_axis"), STUB_THEME as any);
    expect((cfg.data.datasets[0] as any).yAxisID).toBe("y");
    expect((cfg.data.datasets[1] as any).yAxisID).toBe("y1");
    expect((cfg.options as any).scales.y1).toBeDefined();
  });
});


// CTO review P1-R1.2 (#298): all five builders should accept geo column type for x-axis.
describe("#298 geo col type accepted by all builders (省份/城市排名场景)", () => {
  function _geoSpec(kind: ChartSpec["kind"]): ChartSpec {
    return {
      kind, title: "各省份销售额",
      columns: [
        { key: "省", header: "省", type: "geo" },
        { key: "营业额", header: "营业额", type: "numeric" },
      ],
      rows: [
        { 省: "上海", 营业额: 9326 },
        { 省: "北京", 营业额: 9258 },
      ],
      row_count: 2,
    };
  }

  test.each(["bar", "hbar", "stacked_bar", "pie"] as const)(
    "%s build 不抛 (geo 当 categorical x-col)",
    (kind) => {
      expect(() => buildChartJSConfig(_geoSpec(kind), STUB_THEME as any))
        .not.toThrow();
    },
  );

  test("line: 有 date 优先 date, 无 date 但有 geo 也不抛", () => {
    expect(() => buildChartJSConfig(_geoSpec("line"), STUB_THEME as any))
      .not.toThrow();
  });

  test("bar: x-axis labels 来自 geo col rows", () => {
    const cfg = buildChartJSConfig(_geoSpec("bar"), STUB_THEME as any);
    expect(cfg.data.labels).toEqual(["上海", "北京"]);
  });
});

describe("dual_axis/combo date x-col (prod t_2e569479 regression)", () => {
  // A monthly dual-axis trend (每月=date) used to throw in buildDualAxis
  // (x-col finder only matched categorical/geo) → ChartBlock caught it and
  // silently degraded to a single-column ChartTable. Must now render a chart.
  function _dateSpec(kind: ChartSpec["kind"]): ChartSpec {
    return {
      kind, title: "月度拜访 vs 销售达成金额",
      columns: [
        { key: "month", header: "每月", type: "date" },
        { key: "visits", header: "拜访次数", type: "numeric" },
        { key: "sales", header: "销售达成金额", type: "numeric" },
      ],
      rows: [
        { month: "2025年4月", visits: 2647, sales: 110000000 },
        { month: "2025年5月", visits: 2805, sales: 120000000 },
        { month: "2025年6月", visits: 3122, sales: 130000000 },
      ],
      row_count: 3,
    };
  }

  test.each(["dual_axis", "combo"] as const)(
    "%s with date x-col does NOT throw / fall back to table",
    (kind) => {
      expect(() => buildChartJSConfig(_dateSpec(kind), STUB_THEME as any)).not.toThrow();
    },
  );

  test("dual_axis: date col is the x-axis, 2 numeric series rendered", () => {
    const cfg = buildChartJSConfig(_dateSpec("dual_axis"), STUB_THEME as any);
    expect(cfg.data.labels).toEqual(["2025年4月", "2025年5月", "2025年6月"]);
    expect(cfg.data.datasets).toHaveLength(2);
  });

  test("bar: date col is accepted as the x-axis", () => {
    const cfg = buildChartJSConfig(_dateSpec("bar"), STUB_THEME as any);
    expect(cfg.data.labels).toEqual(["2025年4月", "2025年5月", "2025年6月"]);
    expect(cfg.data.datasets[0].label).toBe("拜访次数");
  });
});

describe("chart hover interaction", () => {
  test("groups visible trend values with no active hover animation", () => {
    const cfg = buildChartJSConfig(_spec("line"), STUB_THEME as any);
    expect((cfg.options as any).interaction).toMatchObject({
      mode: "index",
      axis: "x",
      intersect: false,
      includeInvisible: false,
    });
    expect((cfg.options as any).plugins.tooltip).toMatchObject({
      mode: "index",
      axis: "x",
      intersect: false,
    });
    expect((cfg.options as any).transitions.active.animation.duration).toBe(0);
  });

  test.each(["line", "area_stacked"] as const)("%s hover compares one category and omits hidden or missing values", (kind) => {
    const cfg = buildChartJSConfig(_spec(kind, {
      rows: [
        { quarter: "Q1", target: 100, actual: 40, forecast: 0 },
        { quarter: "Q2", target: 110, actual: 0, forecast: 20 },
        { quarter: "Q3", target: 120, actual: null, forecast: 0 },
      ],
      row_count: 3,
    }), STUB_THEME as any, { hiddenSeries: ["forecast"] });
    // Supply measured point centers only; Chart.js selects the active tooltip items.
    const metas = cfg.data.datasets.map((dataset, index) => ({
      index,
      controller: { _cachedMeta: {} },
      data: dataset.data.map((value, row) => new PointElement({
        x: 20 + row * 50,
        y: 20 + index * 30,
        skip: value === null,
        options: { radius: 3, hitRadius: 1 },
      })),
    }));
    const chart = {
      getSortedVisibleDatasetMetas: () => metas.filter((meta) => !(cfg.data.datasets[meta.index] as { hidden?: boolean }).hidden),
      isPointInArea: () => true,
      chartArea: { left: 0, top: 0, right: 200, bottom: 200 },
    } as unknown as Chart;
    const readCategory = (row: number) => {
      const event = {
        type: "mousemove",
        x: 20 + row * 50,
        y: 35,
        native: null,
      } as ChartEvent;
      const options = cfg.options.interaction!;
      return Interaction.modes[options.mode!](chart, event, options, false)
        .map((item) => ({
          label: cfg.data.datasets[item.datasetIndex].label,
          value: cfg.data.datasets[item.datasetIndex].data[item.index],
          category: cfg.data.labels![item.index],
        }));
    };
    expect(readCategory(0)).toEqual([
      { label: "Target", value: 100, category: "Q1" },
      { label: "Actual", value: 40, category: "Q1" },
    ]);
    expect(readCategory(1)).toEqual([
      { label: "Target", value: 110, category: "Q2" },
      { label: "Actual", value: 0, category: "Q2" },
    ]);
    expect(readCategory(2)).toEqual([
      { label: "Target", value: 120, category: "Q3" },
    ]);
  });

  test.each(["scatter", "bar"] as const)("%s hover keeps individual mark selection", (kind) => {
    const cfg = buildChartJSConfig(_spec(kind), STUB_THEME as any);
    expect(cfg.options.interaction).toMatchObject({ mode: "nearest", intersect: true });
    expect(cfg.options.plugins?.tooltip).toMatchObject({ mode: "nearest", intersect: true });
  });
});

describe("preset render context projection", () => {
  test("all chart themes consume render context and accessibility", () => {
    const accessibleTheme = {
      ...STUB_THEME,
      accessibility: { contrast: "high", colorVision: "color-safe" },
    };
    const cfg = buildChartJSConfig(_spec("line"), accessibleTheme as any, {
      surface: "chat",
      renderContext: COMPACT_CONTEXT,
    });
    expect((cfg.options as any).interaction).toMatchObject({ mode: "index", intersect: false });
    // These fixtures use a **category** x column; category axes no longer obey `maxXTicks` (omitted labels lose identity).
    // Compact recipe still affects time/value axes. See the "类目轴刻度上限" suite.
    expect((cfg.options as any).scales.x.ticks.maxTicksLimit).toBeUndefined();
    expect((cfg.options as any).plugins.legend.labels.usePointStyle).toBe(true);
    expect((cfg.data.datasets[1] as any).borderDash).toBeDefined();

    const oneSeries = _oneSeries(8, { kind: "line" });
    const oneSeriesCfg = buildChartJSConfig(oneSeries, accessibleTheme as any, {
      renderContext: COMPACT_CONTEXT,
    });
    const display = (oneSeriesCfg.data.datasets[0] as any).datalabels.display;
    expect(display({ dataIndex: 0 })).toBe(false);
  });

  test("export consumes the frozen static render recipe", () => {
    const cfg = buildChartJSConfig(_oneSeries(8, { kind: "line" }), PRESET_THEME as any, {
      surface: "export",
      renderContext: {
        ...COMPACT_CONTEXT,
        output: { medium: "raster", static: true },
      },
    });
    expect((cfg.options as any).interaction).toMatchObject({ mode: "index", intersect: false });
    // These fixtures use a **category** x column; category axes no longer obey `maxXTicks` (omitted labels lose identity).
    // Compact recipe still affects time/value axes. See the "类目轴刻度上限" suite.
    expect((cfg.options as any).scales.x.ticks.maxTicksLimit).toBeUndefined();
    expect(shown((cfg.data.datasets[0] as any).datalabels.display({ dataIndex: 0 }))).toBe(true);
  });

  test("compact coarse-pointer charts use readable ticks and non-intersecting tooltips", () => {
    const cfg = buildChartJSConfig(_spec("bar"), PRESET_THEME as any, {
      surface: "chat",
      forceDataLabels: true,
      renderContext: COMPACT_CONTEXT,
    });
    expect((cfg.options as any).interaction).toMatchObject({ mode: "index", intersect: false });
    // These fixtures use a **category** x column; category axes no longer obey `maxXTicks` (omitted labels lose identity).
    // Compact recipe still affects time/value axes. See the "类目轴刻度上限" suite.
    expect((cfg.options as any).scales.x.ticks.maxTicksLimit).toBeUndefined();
    expect((cfg.options as any).scales.y.ticks.maxTicksLimit).toBe(4);
    expect((cfg.options as any).layout.padding).toEqual({ left: 12, right: 16 });
  });

  test("single-series trends drop the redundant legend while preserving explicit overrides", () => {
    const one = _oneSeries(8);
    one.kind = "line";
    const automatic = buildChartJSConfig(one, PRESET_THEME as any, {
      renderContext: COMPACT_CONTEXT,
    });
    expect((automatic.options as any).plugins.legend.display).toBe(false);

    const explicit = buildChartJSConfig({ ...one, show_legend: true }, PRESET_THEME as any, {
      renderContext: COMPACT_CONTEXT,
    });
    // A redundant single-series legend remains hidden even when the old field is true;
    // the field may only suppress, not weaken the preset readability recipe.
    expect((explicit.options as any).plugins.legend.display).toBe(false);
  });

  test("color-safe multi-series lines also differ by dash and marker", () => {
    const colorSafe = {
      ...PRESET_THEME,
      accessibility: { contrast: "standard", colorVision: "color-safe" },
    };
    const cfg = buildChartJSConfig(_spec("line"), colorSafe as any, {
      renderContext: COMPACT_CONTEXT,
    });
    const datasets = cfg.data.datasets as any[];
    expect(datasets[0].pointStyle).not.toBe(datasets[1].pointStyle);
    expect(datasets[1].borderDash.length).toBeGreaterThan(0);
    expect((cfg.options as any).plugins.legend.labels.usePointStyle).toBe(true);
  });

  test("compact dense line charts suppress overlapping endpoint labels", () => {
    const sevenSeries: ChartSpec = {
      kind: "line",
      title: "seven regions",
      row_count: 6,
      columns: [
        { key: "month", header: "Month", type: "date" },
        ...Array.from({ length: 7 }, (_, index) => ({
          key: `region_${index}`,
          header: `Region ${index + 1}`,
          type: "numeric" as const,
        })),
      ],
      rows: Array.from({ length: 6 }, (_, rowIndex) => ({
        month: `2026-0${rowIndex + 1}`,
        ...Object.fromEntries(Array.from({ length: 7 }, (_, seriesIndex) => [
          `region_${seriesIndex}`,
          1_000 + seriesIndex * 500 + rowIndex * 100,
        ])),
      })),
    };
    const cfg = buildChartJSConfig(sevenSeries, PRESET_THEME as any, {
      forceDataLabels: true,
      renderContext: COMPACT_CONTEXT,
    });

    for (const dataset of cfg.data.datasets as any[]) {
      expect(dataset.datalabels.display({ dataIndex: 0 })).toBe(false);
      expect(dataset.datalabels.display({ dataIndex: 5 })).toBe(false);
    }

    const explicit = buildChartJSConfig(
      { ...sevenSeries, show_data_labels: true },
      PRESET_THEME as any,
      { renderContext: COMPACT_CONTEXT },
    );
    expect(shown((explicit.data.datasets[0] as any).datalabels.display({ dataIndex: 0 }))).toBe(true);
  });

  test("reduced-motion context disables chart animation", () => {
    const cfg = buildChartJSConfig(_spec("bar"), PRESET_THEME as any, {
      renderContext: { ...COMPACT_CONTEXT, reducedMotion: true },
    });
    expect((cfg.options as any).animation.duration).toBe(0);
  });
});

// ─────────────────────────────────────────────────────────────
// WP2 (design D3): waterfall / sankey / heatmap builders.
// ─────────────────────────────────────────────────────────────

const WP2_THEME = {
  ...STUB_THEME,
  palette: ["#b04a2a", "#5e7d8a", "#a98028", "#7a5a3f", "#9a8a6a", "#3a342c"],
  sequential: { from: "#faf0e8", to: "#b04a2a" },
  positive: "#3a724a",
  negative: "#9f2438",
} as never;

describe("buildWaterfall (kind=waterfall)", () => {
  const spec: ChartSpec = {
    kind: "waterfall", title: "EBITDA bridge", row_count: 4,
    columns: [
      { key: "item", header: "分项", type: "categorical" },
      { key: "delta", header: "增量", type: "numeric" },
    ],
    rows: [
      { item: "Volume", delta: 4.1 },
      { item: "Price", delta: 2.8 },
      { item: "Mix", delta: -1.2 },
      { item: "FX", delta: -0.9 },
    ],
  };

  test("floating-bar 区间 + Σ 合计 bar", () => {
    const cfg = buildChartJSConfig(spec, WP2_THEME);
    expect(cfg.type).toBe("bar");
    const data = cfg.data.datasets[0].data as Array<[number, number]>;
    expect(cfg.data.labels).toEqual(["Volume", "Price", "Mix", "FX", "Σ"]);
    expect(data[0]).toEqual([0, 4.1]);            // +4.1: 0→4.1
    expect(data[1][0]).toBeCloseTo(4.1);          // +2.8: 4.1→6.9
    expect(data[1][1]).toBeCloseTo(6.9);
    expect(data[2][0]).toBeCloseTo(6.9);          // -1.2: starts at 6.9, ends at 5.7 (start → end, not low → high).
    expect(data[2][1]).toBeCloseTo(5.7);
    expect(data[4][1]).toBeCloseTo(4.8);          // Σ = 4.1+2.8-1.2-0.9
  });

  // Readability audit (13 waterfalls): datalabels orients vertical bars upward whatever their sign, so a fixed
  // `anchor/align: "end"` put every drop's label on its top edge — level with the previous step's lower end, where it
  // read as that bar's label — and a first drop from zero above the plot, clipped.
  test("a rise is labelled above its bar, a drop below its own bar", () => {
    const cfg = buildChartJSConfig({ ...spec, show_data_labels: true }, WP2_THEME);
    const dl = (cfg.data.datasets[0] as any).datalabels;
    const placement = (i: number) => [dl.anchor({ dataIndex: i }), dl.align({ dataIndex: i })];
    expect(placement(0)).toEqual(["end", "end"]); // +4.1
    expect(placement(2)).toEqual(["start", "start"]); // -1.2
    expect(placement(4)).toEqual(["end", "end"]); // Σ = +4.8
    const data = cfg.data.datasets[0].data as Array<[number, number]>;
    for (const [i, row] of (spec.rows ?? []).entries()) {
      expect(data[i]![1] - data[i]![0]).toBeCloseTo(row.delta as number); // [start of step, end of step]
    }
  });

  test("a negative total is labelled below its bar too", () => {
    const neg: ChartSpec = { ...spec, show_data_labels: true, rows: [{ item: "A", delta: 2 }, { item: "B", delta: -7 }] };
    const dl = (buildChartJSConfig(neg, WP2_THEME).data.datasets[0] as any).datalabels;
    expect(dl.anchor({ dataIndex: 2 })).toBe("start");
  });

  test("a negative total ends below zero and the tooltip reports it signed", () => {
    const neg: ChartSpec = { ...spec, rows: [{ item: "A", delta: 2 }, { item: "B", delta: -7 }] };
    const cfg = buildChartJSConfig(neg, WP2_THEME);
    const data = cfg.data.datasets[0].data as Array<[number, number]>;
    expect(data[2]).toEqual([0, -5]);
    const label = (cfg.options as any).plugins.tooltip.callbacks.label;
    expect(label({ raw: data[2], dataIndex: 2 })).toMatch(/^-5$/);
  });

  test("labels get value-axis headroom; unlabelled charts keep their axis", () => {
    const labelled = buildChartJSConfig({ ...spec, show_data_labels: true }, WP2_THEME);
    expect((labelled.options as any).scales?.y?.grace).toBe("8%");
    const bare = buildChartJSConfig({ ...spec, show_data_labels: false }, WP2_THEME);
    expect((bare.options as any).scales?.y?.grace).toBeUndefined();
  });

  test("批1 2.1b 语义色: 正=positive 负=negative Σ=palette[0] 主角（D5 waterfall 行）", () => {
    const cfg = buildChartJSConfig(spec, WP2_THEME);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const colors = (cfg.data.datasets[0] as any).backgroundColor as string[];
    expect(colors[0]).toBe("#3a724a");   // +4.1 → semantic green (outside series palette).
    expect(colors[2]).toBe("#9f2438");   // -1.2 → semantic red.
    expect(colors[4]).toBe("#b04a2a");   // Σ → protagonist terracotta (structural lead).
  });
});

describe("buildSankey (kind=sankey)", () => {
  const spec: ChartSpec = {
    kind: "sankey", title: "渠道→区域", row_count: 3,
    columns: [
      { key: "ch", header: "渠道", type: "categorical" },
      { key: "rg", header: "区域", type: "categorical" },
      { key: "rev", header: "营收", type: "numeric" },
    ],
    rows: [
      { ch: "Direct", rg: "AMER", rev: 30.4 },
      { ch: "Partner", rg: "EMEA", rev: 18.6 },
      { ch: "Direct", rg: "EMEA", rev: 0 },     // Filter flow=0 links.
    ],
  };

  test("links 转换 + 0 流量过滤", () => {
    const cfg = buildChartJSConfig(spec, WP2_THEME);
    expect(cfg.type).toBe("sankey");
    const links = cfg.data.datasets[0].data as Array<{ from: string; to: string; flow: number }>;
    expect(links).toHaveLength(2);
    expect(links[0]).toMatchObject({ from: "Direct", to: "AMER", flow: 30.4 });
  });

  test("列不足时抛错 (ErrorBoundary 接表格降级)", () => {
    const bad = { ...spec, columns: spec.columns.slice(1) };
    expect(() => buildChartJSConfig(bad, WP2_THEME)).toThrow(/sankey needs/);
  });
});

describe("buildHeatmap (kind=heatmap)", () => {
  const spec: ChartSpec = {
    kind: "heatmap", title: "区域×月", row_count: 4,
    columns: [
      { key: "m", header: "月", type: "categorical" },
      { key: "rg", header: "区域", type: "categorical" },
      { key: "v", header: "指数", type: "numeric" },
    ],
    rows: [
      { m: "J", rg: "DACH", v: 62 },
      { m: "F", rg: "DACH", v: 99 },
      { m: "J", rg: "Nordics", v: 31 },
      { m: "F", rg: "Nordics", v: 48 },
    ],
  };

  test("matrix cells + category 双轴", () => {
    const cfg = buildChartJSConfig(spec, WP2_THEME);
    expect(cfg.type).toBe("matrix");
    const cells = cfg.data.datasets[0].data as Array<{ x: string; y: string; v: number }>;
    expect(cells).toHaveLength(4);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const scales = cfg.options.scales as any;
    expect(scales.x.labels).toEqual(["J", "F"]);
    expect(scales.y.labels).toEqual(["DACH", "Nordics"]);
  });

  test("sequential 上色: vmax cell 为 to 色, vmin 为 from 色", () => {
    const cfg = buildChartJSConfig(spec, WP2_THEME);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const bg = (cfg.data.datasets[0] as any).backgroundColor;
    const colorAt = (i: number) => bg({ dataset: { data: cfg.data.datasets[0].data }, dataIndex: i });
    expect(colorAt(1)).toBe("rgb(176,74,42)");    // v=99 (max) → #b04a2a
    expect(colorAt(2)).toBe("rgb(250,240,232)");  // v=31 (min) → #faf0e8
  });
});


describe("buildBar stacked_bar pivot (飞书07.06 #3)", () => {
  // Two category dimensions (一级分行 × 获取渠道) + two measures (刷卡金额 ~million / 客户数 ~thousand).
  // Actual device shape (report "前三分行渠道分布对比").
  const _pivotSpec = (extras: Partial<ChartSpec> = {}): ChartSpec => ({
    kind: "stacked_bar", title: "分行渠道",
    columns: [
      { key: "branch", header: "一级分行", type: "categorical" },
      { key: "channel", header: "获取渠道", type: "categorical" },
      { key: "amount", header: "刷卡金额", type: "numeric" },
      { key: "custn", header: "客户数", type: "numeric" },
    ],
    rows: [
      { branch: "广西", channel: "线下", amount: 781, custn: 2872 },
      { branch: "广西", channel: "网上", amount: 737, custn: 2720 },
      { branch: "海南", channel: "线下", amount: 777, custn: 2818 },
      { branch: "海南", channel: "网上", amount: 747, custn: 2740 },
    ],
    row_count: 4,
    ...extras,
  });

  test("pivot 维度2 成堆叠系列(渠道), 不再把两个指标堆一根柱", () => {
    const cfg = buildChartJSConfig(_pivotSpec(), STUB_THEME as any);
    // x axis = deduplicated dimension 1 (branches).
    expect(cfg.data.labels).toEqual(["广西", "海南"]);
    // Series = deduplicated dimension 2 (channels), not the two measures.
    const labels = (cfg.data.datasets as any[]).map((d) => d.label);
    expect(labels).toEqual(["线下", "网上"]);
    // Segment values = primary measure (default first numeric column=刷卡金额), excluding 客户数.
    const off = (cfg.data.datasets as any[]).find((d) => d.label === "线下");
    expect(off.data).toEqual([781, 777]); // Card amounts for Guangxi offline / Hainan offline.
    // Never stack "客户数" as a series.
    expect(labels).not.toContain("客户数");
    expect(labels).not.toContain("刷卡金额");
  });

  test("sort_by 指定主指标时按它取值(客户数)", () => {
    const cfg = buildChartJSConfig(_pivotSpec({ sort_by: "custn" }), STUB_THEME as any);
    const off = (cfg.data.datasets as any[]).find((d) => d.label === "线下");
    expect(off.data).toEqual([2872, 2818]); // Switch primary measure to 客户数.
  });

  test("review[5]: 同一(维度1,维度2)多行 → 聚合求和(不后写覆盖)", () => {
    const spec: ChartSpec = {
      kind: "stacked_bar", title: "聚合",
      columns: [
        { key: "branch", header: "分行", type: "categorical" },
        { key: "channel", header: "渠道", type: "categorical" },
        { key: "amount", header: "金额", type: "numeric" },
      ],
      rows: [
        { branch: "广西", channel: "线下", amount: 400 },
        { branch: "广西", channel: "线下", amount: 381 }, // Same (广西,线下) → 781.
        { branch: "广西", channel: "网上", amount: 737 },
        { branch: "海南", channel: "线下", amount: 777 },
      ],
      row_count: 4,
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    const off = (cfg.data.datasets as any[]).find((d) => d.label === "线下");
    expect(off.data).toEqual([781, 777]); // 广西线下=400+381 accumulates, rather than overwriting with 381.
  });

  test("review[2]: 非堆叠 bar, [categorical, date, numeric] → x 轴按列序取 categorical(非 date)", () => {
    const spec: ChartSpec = {
      kind: "bar", title: "x序",
      columns: [
        { key: "branch", header: "分行", type: "categorical" },
        { key: "month", header: "月份", type: "date" },
        { key: "sales", header: "销量", type: "numeric" },
      ],
      rows: [
        { branch: "广西", month: "2026-01", sales: 10 },
        { branch: "海南", month: "2026-02", sales: 14 },
      ],
      row_count: 2,
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    expect(cfg.data.labels).toEqual(["广西", "海南"]); // date does not take priority; branches form x axis.
  });

  test("单维度多指标(同单位分项)仍分别成 dataset(不 pivot)", () => {
    // One category column → no pivot; preserve original component stacking.
    const cfg = buildChartJSConfig(_spec("stacked_bar"), STUB_THEME as any);
    expect((cfg.data.datasets as any[]).map((d) => d.label))
      .toEqual(["Target", "Actual", "Forecast"]);
  });
});


// ─────────────────────────────────────────────────────────────
// Batch 1 2.2/2.3 (chart-table-ux): data-label policy + axis units + heatmap legend.
// ─────────────────────────────────────────────────────────────

const THEME_SEQ = {
  ...STUB_THEME,
  sequential: { from: "#faf0e8", to: "#b04a2a" },
} as any;

function _oneSeries(nRows: number, extras: Partial<ChartSpec> = {}): ChartSpec {
  return {
    kind: "bar", title: "标签策略",
    columns: [
      { key: "cat", header: "品类", type: "categorical" },
      { key: "v", header: "营业额", type: "numeric", unit: "万元" },
    ],
    rows: Array.from({ length: nRows }, (_, i) => ({ cat: `类${i}`, v: 10 + i })),
    row_count: nRows,
    ...extras,
  };
}

describe("批1 2.2 resolveDataLabels 策略表", () => {
  const dl = (cfg: ReturnType<typeof buildChartJSConfig>) =>
    (cfg.data.datasets[0] as any).datalabels;

  test("默认：单系列 ≤12 类 → 开", () => {
    expect(shown(dl(buildChartJSConfig(_oneSeries(8), STUB_THEME as any)).display)).toBe(true);
  });

  test("默认：单系列 >12 类 → 关（soft guideline）", () => {
    expect(dl(buildChartJSConfig(_oneSeries(20), STUB_THEME as any)).display).toBe(false);
  });

  test("P1-5：spec.show_data_labels 显式值胜出（20 类也开 / 5 类也关）", () => {
    expect(shown(dl(buildChartJSConfig(_oneSeries(20, { show_data_labels: true }), STUB_THEME as any)).display)).toBe(true);
    expect(dl(buildChartJSConfig(_oneSeries(5, { show_data_labels: false }), STUB_THEME as any)).display).toBe(false);
  });

  test("导出面恒开（PRD §3.9 静态可读）；显式关仍可关", () => {
    expect(shown(dl(buildChartJSConfig(_oneSeries(20), STUB_THEME as any, { surface: "export" })).display)).toBe(true);
    expect(dl(buildChartJSConfig(_oneSeries(20, { show_data_labels: false }), STUB_THEME as any, { surface: "export" })).display).toBe(false);
  });

  test("移动端 forceDataLabels → 开", () => {
    expect(shown(dl(buildChartJSConfig(_oneSeries(20), STUB_THEME as any, { forceDataLabels: true })).display)).toBe(true);
  });

  test("折线只标端点+极值：中段普通点 display=false", () => {
    const spec: ChartSpec = {
      kind: "line", title: "端点极值",
      columns: [
        { key: "m", header: "月份", type: "date" },
        { key: "v", header: "营业额", type: "numeric" },
      ],
      // first=0, max=1, min=3, last=4 → index 2 is an ordinary middle point.
      rows: [
        { m: "2026-01", v: 2 }, { m: "2026-02", v: 9 }, { m: "2026-03", v: 4 },
        { m: "2026-04", v: 1 }, { m: "2026-05", v: 5 },
      ],
      row_count: 5,
    };
    const d = dl(buildChartJSConfig(spec, STUB_THEME as any));
    expect(shown(d.display({ dataIndex: 0 }))).toBe(true);
    expect(shown(d.display({ dataIndex: 1 }))).toBe(true);
    expect(shown(d.display({ dataIndex: 2 }))).toBe(false);
    expect(shown(d.display({ dataIndex: 3 }))).toBe(true);
    expect(shown(d.display({ dataIndex: 4 }))).toBe(true);
  });

  test("堆叠 pivot：只有最上层标列总高，且是聚合值", () => {
    const spec: ChartSpec = {
      kind: "stacked_bar", title: "堆叠总高",
      columns: [
        { key: "branch", header: "分行", type: "categorical" },
        { key: "channel", header: "渠道", type: "categorical" },
        { key: "amt", header: "金额", type: "numeric" },
      ],
      rows: [
        { branch: "北京", channel: "网点", amt: 4 },
        { branch: "北京", channel: "线上", amt: 3 },
        { branch: "上海", channel: "网点", amt: 2 },
        { branch: "上海", channel: "线上", amt: 1 },
      ],
      row_count: 4,
      show_data_labels: true,
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    const dss = cfg.data.datasets as any[];
    expect(dss[0].datalabels.display).toBe(false);
    const top = dss[dss.length - 1].datalabels;
    expect(shown(top.display)).toBe(true);
    // 北京 column total height = 4+3.
    expect(top.formatter(null, { dataIndex: 0 })).toContain("7");
  });
});

describe("批1 2.3 轴单位/轴标题 + 热力图图例", () => {
  test("bar 值轴：有 unit → title 只写单位、放轴末端；无 unit 且多列不同名 → 不渲 title", () => {
    const withUnit = buildChartJSConfig(_oneSeries(5), STUB_THEME as any);
    // Chart Embed Spec: the measure name is in the chart title / legend; the axis states only its dimension.
    expect(((withUnit.options as any).scales.y.title).text).toBe("万元");
    expect(((withUnit.options as any).scales.y.title).align).toBe("end");
    const noUnit = buildChartJSConfig(_spec("bar"), STUB_THEME as any);
    expect((noUnit.options as any).scales.y.title).toBeUndefined();
  });

  test("dual_axis 单列轴恒带名（无 unit 也显示 header）；多列轴不冒充", () => {
    // `_spec` has three numeric columns ⇒ left axis [Target], right axis [Actual, Forecast].
    // This assertion previously required `y1.title.text === "Actual"`, precisely the misleading title being fixed:
    // right axis carries both Actual and Forecast, yet names only the former.
    const cfg = buildChartJSConfig(_spec("dual_axis"), STUB_THEME as any);
    expect(((cfg.options as any).scales.y.title).display).toBe(true);
    expect(((cfg.options as any).scales.y.title).text).toBe("Target");
    expect((cfg.options as any).scales.y1.title).toBeUndefined();
  });

  // ── dual_axis multi-column axis names: 116 measured 53 charts / 106 axes, with 17 multi-column axes;
  //    none shared measure_header ⇒ original `axisTitleOf(cols[0], theme, true)`
  //    used one column's name to impersonate the whole axis on all 17.
  //    Fixture column-name/dimension combinations come from real 116 specs.
  describe("dual_axis 多列轴不拿第一列冒充整根轴", () => {
    function _multi(right: Partial<ChartColumn>[]): ChartSpec {
      return {
        kind: "dual_axis", title: "多列右轴",
        columns: [
          { key: "店", header: "门店", type: "categorical" },
          { key: "m0", header: "营业额", type: "numeric" },
          ...right.map((c, i) => ({ key: `r${i}`, type: "numeric" as const, ...c })) as ChartColumn[],
        ],
        rows: [
          { 店: "东湖街", m0: 120, r0: 1.2, r1: 3.4 },
          { 店: "马家堡", m0: 90, r0: 2.1, r1: 1.8 },
        ],
        row_count: 2,
      };
    }

    test("量纲一致（全百分比）→ 只标量纲 %，不写任一列名", () => {
      const cfg = buildChartJSConfig(_multi([
        { header: "营业额环比变化率", format: "percent", percent_scale: "points" },
        { header: "利润率", format: "percent", percent_scale: "fraction" },
      ]), STUB_THEME as any);
      expect(((cfg.options as any).scales.y1.title).text).toBe("%");
      // Left axis remains single-column → retain original name.
      expect(((cfg.options as any).scales.y.title).text).toBe("营业额");
    });

    test("量纲一致（同 unit）→ 只标该 unit", () => {
      const cfg = buildChartJSConfig(_multi([
        { header: "利润", unit: "¥" },
        { header: "月坪效", unit: "¥" },
      ]), STUB_THEME as any);
      expect(((cfg.options as any).scales.y1.title).text).toBe("¥");
    });

    test("量纲不一致（人次 vs 元）→ 不标，而不是写成「单店客流量」", () => {
      const cfg = buildChartJSConfig(_multi([
        { header: "单店客流量", unit: "人次" },
        { header: "单店IPT", unit: "元" },
      ]), STUB_THEME as any);
      expect((cfg.options as any).scales.y1.title).toBeUndefined();
    });

    test("多列但共享 measure_header → 仍按度量名标（原行为不变）", () => {
      const cfg = buildChartJSConfig(_multi([
        { header: "华东", measure_header: "营业额", unit: "元" },
        { header: "华南", measure_header: "营业额", unit: "元" },
      ]), STUB_THEME as any);
      expect(((cfg.options as any).scales.y1.title).text).toBe("营业额（元）");
    });

    test("单列轴不受影响：两根轴各一列时照旧带名", () => {
      const cfg = buildChartJSConfig(_multi([
        { header: "客流量", unit: "人次" },
      ]), STUB_THEME as any);
      expect(((cfg.options as any).scales.y.title).text).toBe("营业额");
      expect(((cfg.options as any).scales.y1.title).text).toBe("客流量（人次）");
    });
  });

  test("heatmap 带 heatmapLegend（真实 min/max + 主题 sequential 两端色）", () => {
    const spec: ChartSpec = {
      kind: "heatmap", title: "热力",
      columns: [
        { key: "x", header: "门店", type: "categorical" },
        { key: "y", header: "星期", type: "categorical" },
        { key: "v", header: "人次", type: "numeric" },
      ],
      rows: [
        { x: "A", y: "一", v: 31 }, { x: "A", y: "二", v: 99 },
        { x: "B", y: "一", v: 55 }, { x: "B", y: "二", v: 42 },
      ],
      row_count: 4,
    };
    const cfg = buildChartJSConfig(spec, THEME_SEQ);
    expect(cfg.heatmapLegend).toMatchObject({ min: 31, max: 99, from: "#faf0e8", to: "#b04a2a" });
  });

  test("waterfall 标签：分项带符号、Σ 总额不带符号", () => {
    const spec: ChartSpec = {
      kind: "waterfall", title: "毛利桥",
      columns: [
        { key: "item", header: "项", type: "categorical" },
        { key: "delta", header: "增量", type: "numeric", unit: "亿" },
      ],
      rows: [
        { item: "价格", delta: 6.1 },
        { item: "销量", delta: -3.4 },
      ],
      row_count: 2,
      show_data_labels: true,
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    const f = (cfg.data.datasets[0] as any).datalabels.formatter;
    expect(f(null, { dataIndex: 0 })).toMatch(/^\+/);
    expect(f(null, { dataIndex: 1 })).toMatch(/^−/);
    // Final element = Σ (two component rows → index 2), total 2.7, without a sign.
    expect(f(null, { dataIndex: 2 })).not.toMatch(/^[+−]/);
  });
});


describe("批3 4.2 pie 图例带占比（稿 C v2 C5）", () => {
  const pieSpec: ChartSpec = {
    kind: "pie", title: "渠道占比",
    columns: [
      { key: "ch", header: "渠道", type: "categorical" },
      { key: "v", header: "金额", type: "numeric" },
    ],
    rows: [
      { ch: "直营", v: 60 },
      { ch: "加盟", v: 30 },
      { ch: "电商", v: 10 },
    ],
    row_count: 3,
  };

  function legendItems(cfg: ReturnType<typeof buildChartJSConfig>) {
    const gen = (cfg.options as any).plugins.legend.labels.generateLabels;
    const chart = {
      data: cfg.data,
      getDataVisibility: () => true,
    };
    return gen(chart) as Array<{ text: string; fillStyle: string }>;
  }

  test("图例文案 = 名称 + 占比（不再只写名字让用户目测面积）", () => {
    const items = legendItems(buildChartJSConfig(pieSpec, STUB_THEME as any));
    expect(items.map((i) => i.text)).toEqual(["直营 60%", "加盟 30%", "电商 10%"]);
  });

  test("色块跟数据集配色一致（主角满色/其余退浅由 2.1b 决定）", () => {
    const cfg = buildChartJSConfig(pieSpec, STUB_THEME as any);
    const colors = (cfg.data.datasets[0] as any).backgroundColor as string[];
    expect(legendItems(cfg).map((i) => i.fillStyle)).toEqual(colors);
  });

  test("全零数据不产生 NaN%（退回纯名称）", () => {
    const zero = { ...pieSpec, rows: [{ ch: "A", v: 0 }, { ch: "B", v: 0 }] };
    expect(legendItems(buildChartJSConfig(zero, STUB_THEME as any)).map((i) => i.text)).toEqual(["A", "B"]);
  });

  // Readability audit (pie, 71 charts): fading non-lead slices 70% toward the surface collapsed distinct hues into
  // near-identical pastels the legend could no longer map back. A slice's color is its identity.
  test("every slice keeps its full series color, even with a protagonist", () => {
    const cfg = buildChartJSConfig(pieSpec, STUB_THEME as any, { captionText: "加盟渠道占三成" });
    const ds = cfg.data.datasets[0] as any;
    expect(ds.backgroundColor).toEqual(STUB_THEME.palette.slice(0, 3));
  });

  test("the protagonist is pulled out of the ring instead", () => {
    const lead = buildChartJSConfig(pieSpec, STUB_THEME as any, { captionText: "加盟渠道占三成" });
    expect((lead.data.datasets[0] as any).offset).toEqual([0, 8, 0]);
    // Without a caption the largest slice is the protagonist (structural pie lead).
    const largest = buildChartJSConfig(pieSpec, STUB_THEME as any);
    expect((largest.data.datasets[0] as any).offset).toEqual([8, 0, 0]);
  });
});


// ─────────────────────────────────────────────────────────────
// review R0-27 / R1-7 / R2-5: chart formatting and aggregation honesty (previously uncovered).
// ─────────────────────────────────────────────────────────────

describe("R0-27 图表面必须与表格同口径", () => {
  const pctSpec: ChartSpec = {
    kind: "bar", title: "利润率",
    columns: [
      { key: "m", header: "月份", type: "categorical" },
      // Actual production shape: LLM supplies dimensionless "decimal" as a unit.
      { key: "v", header: "利润率", type: "numeric", percent_scale: "fraction", unit: "decimal" },
    ],
    rows: [{ m: "1月", v: 0.1744 }, { m: "2月", v: -0.03 }],
    row_count: 2,
    show_data_labels: true,
  };

  test("★数值标签：百分比列出 %，不再是 0.2decimal / -0decimal", () => {
    const cfg = buildChartJSConfig(pctSpec, STUB_THEME as any);
    const f = (cfg.data.datasets[0] as any).datalabels.formatter;
    expect(f(0.1744)).toBe("17.44%");
    expect(f(-0.03)).toBe("-3%");
    expect(f(0.1744)).not.toContain("decimal");
  });

  test("★轴标题：百分比列量纲是 %，不是 LLM 给的 decimal", () => {
    const cfg = buildChartJSConfig(pctSpec, STUB_THEME as any);
    expect(((cfg.options as any).scales.y.title).text).toBe("%");
  });

  test("★tooltip：百分比列带 %（精确档同样不许露无量纲单位）", () => {
    const cfg = buildChartJSConfig(pctSpec, STUB_THEME as any);
    const cb = (cfg.options as any).plugins.tooltip.callbacks.label;
    // R5-1: dataset must be the real one (with `_fmtCtx` pre-attached by buildChartJSConfig).
    // Previously hand-built `{label, _yCol}` + `chart.data: cfg.data` bypassed
    // react-chartjs-2 cloneData in two places, letting an implementation that never worked in browsers pass.
    const ds = (cfg.data.datasets as any[]).find((d) => d._yCol?.key === "v");
    const out = cb({
      dataset: { ...ds },
      parsed: { y: 0.1744 },
      chart: { options: cfg.options, data: { labels: cfg.data.labels, datasets: [{ ...ds }] } },
    });
    expect(out).toBe("利润率: 17.44%");
  });

  // ─── R4-1 / R5-1 ──────────────────────────────────────────────────
  // Tooltip percentage scaling must use the **whole column**. Historically `config.data._rows` supplied scaling
  // to tooltip, but react-chartjs-2 cloneData preserves only labels/datasets; custom keys on `data`
  // are dropped entirely in real rendering, so that path **never worked** (not only newly covered R4-1
  // dual_axis/combo/scatter, but also bar/line). R5-1 pre-attaches scaling
  // to dataset (`setDatasets` spreads `{...ds}`, preserving it).
  // Coverage follows **measurements**: pie and waterfall each have their own tooltip.callbacks.label
  // (share % / increment), bypassing baseOptions; every other kind using baseOptions is covered here.
  // Two numeric columns are required by dual_axis / combo / scatter, so use a separate fixture.
  const twoColPctSpec: ChartSpec = {
    kind: "bar", title: "营业额与利润率",
    columns: [
      { key: "m", header: "月份", type: "categorical" },
      { key: "amt", header: "营业额", type: "numeric", unit: "亿" },
      { key: "v", header: "利润率", type: "numeric", percent_scale: "fraction", unit: "decimal" },
    ],
    rows: [{ m: "1月", amt: 12, v: 0.1744 }, { m: "2月", amt: 9, v: -0.03 }],
    row_count: 2,
  };

  /** Reproduce react-chartjs-2 `cloneData()` (dist/index.js:38): preserve labels and
   *  datasets only, spreading each dataset via `{...ds}`. **Tests must pass through this step**: R4-1
   *  passed `cfg.data` directly as chart.data, bypassing the real failure point and letting an implementation
   *  that never worked in browsers pass every test. */
  function asRenderedChart(cfg: any) {
    return {
      options: cfg.options,
      data: {
        labels: cfg.data.labels,
        datasets: (cfg.data.datasets ?? []).map((ds: any) => ({ ...ds })),
      },
    };
  }

  test.each(["bar", "hbar", "line", "dual_axis", "combo", "scatter"] as const)(
    "★R4-1/R5-1 %s 的 tooltip 走列级百分比档，且档位挺得过 cloneData",
    (kind) => {
      const cfg = buildChartJSConfig({ ...twoColPctSpec, kind }, STUB_THEME as any);
      const chart = asRenderedChart(cfg);
      const cb = (cfg.options as any).plugins.tooltip.callbacks.label;
      // Get dataset from the cloned chart too, exactly as real tooltips do.
      const ds = chart.data.datasets.find((d: any) => d._yCol?.key === "v");
      expect(ds, "利润率 dataset 应带 _yCol").toBeTruthy();
      const out: string = cb({
        dataset: ds,
        parsed: kind === "hbar" ? { x: 0.1744 } : { y: 0.1744 },
        chart,
      });
      // Pin numeric portion only: scatter dataset.label is a category ("1月"), not a column name;
      // kind prefixes already differ; pinning them would enforce unrelated behavior.
      expect(out.endsWith("17.44%"), `Observed tooltip: ${out}`).toBe(true);
      expect(out).not.toContain("decimal");
    },
  );

  test("★R5-1 clone 后 data 上的自定义键会丢，档位不许依赖它", () => {
    const cfg = buildChartJSConfig(twoColPctSpec, STUB_THEME as any);
    const chart = asRenderedChart(cfg);
    expect(Object.keys(chart.data).sort()).toEqual(["datasets", "labels"]);
    const ds: any = chart.data.datasets.find((d: any) => d._yCol?.key === "v");
    expect(ds._fmtCtx, "列级档位必须挂在 dataset 上才能活过 cloneData").toBeTruthy();
    expect(ds._fmtCtx.percent).toBe("fraction");
  });

  test("无量纲单位（decimal/ratio）在非百分比列也不许露出来", () => {
    const spec: ChartSpec = {
      kind: "bar", title: "系数",
      columns: [
        { key: "m", header: "样本", type: "categorical" },
        { key: "v", header: "相关系数", type: "numeric", unit: "correlation" },
      ],
      rows: [{ m: "A", v: 0.51 }],
      row_count: 1,
      show_data_labels: true,
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    expect((cfg.data.datasets[0] as any).datalabels.formatter(0.51)).not.toContain("correlation");
    // Correlation coefficients are not percentages (value range does not determine this); must not become 51%.
    expect((cfg.data.datasets[0] as any).datalabels.formatter(0.51)).not.toContain("%");
    // **False units must remain hidden**: this test has always guarded against `相关系数（correlation）`.
    // A dimensionless column has no unit, so the unit-only axis (Chart Embed Spec) draws no title at all.
    expect((cfg.options as any).scales.y.title).toBeUndefined();
  });

  test("★★ 同一列在图上和表格上必须同档——断言的是**两侧相等**，不是某个字面量", () => {
    // ☠ Previously `expect(f(0.83)).toBe("0.8%")`, despite title "与表格一致".
    //   It pinned precisely the **inconsistent side**: `销售金额达成率` matches the fraction name family,
    //   so table (given column) yields 83.0%, but chart (then lacking column) guessed by values → 0.8%.
    //   Claimed alignment while freezing a 100× difference. Compare both sides directly instead.
    const spec: ChartSpec = {
      kind: "bar", title: "达成率",
      columns: [
        { key: "m", header: "门店", type: "categorical" },
        { key: "v", header: "销售金额达成率", type: "numeric", percent_scale: "fraction" },
      ],
      rows: [{ m: "A", v: 0.83 }, { m: "B", v: 13.5 }],
      row_count: 2,
      show_data_labels: true,
    };
    const col = spec.columns![1]!;
    const values = spec.rows!.map((r) => r.v as number);
    // Table-side semantics (computed this way by chart-table.tsx / chart-inspect.tsx / chart-kpi.tsx).
    const tableScale = resolvePercentScale(values, col);
    const f = (buildChartJSConfig(spec, STUB_THEME as any).data.datasets[0] as any).datalabels.formatter;
    for (const v of values) {
      expect(f(v)).toBe(formatPercentCell(v, tableScale));
    }
    // This column's scale comes from its **name** (tier 2), not the 13.5 among its values.
    expect(tableScale).toBe("fraction");
  });
});

describe("R1-7 聚合数字不得凭空造", () => {
  test("★堆叠柱：量纲不同的多数值列不标总量（营业额_亿 + 利润率_pct = 121.4% 那种）", () => {
    const spec: ChartSpec = {
      kind: "stacked_bar", title: "混量纲",
      columns: [
        { key: "c", header: "城市", type: "categorical" },
        { key: "amt", header: "营业额_亿", type: "numeric", unit: "亿" },
        { key: "rate", header: "利润率_pct", type: "numeric" },
      ],
      rows: [{ c: "上海", amt: 104.06, rate: 17.34 }],
      row_count: 1,
      show_data_labels: true,
    };
    const dss = buildChartJSConfig(spec, STUB_THEME as any).data.datasets as any[];
    expect(dss[dss.length - 1].datalabels.display).toBe(false);
  });

  test("同单位可加时仍标总量（不误伤正常堆叠）", () => {
    const spec: ChartSpec = {
      kind: "stacked_bar", title: "同量纲",
      columns: [
        { key: "c", header: "城市", type: "categorical" },
        { key: "a", header: "线上", type: "numeric", unit: "万元" },
        { key: "b", header: "线下", type: "numeric", unit: "万元" },
      ],
      rows: [{ c: "上海", a: 3, b: 4 }],
      row_count: 1,
      show_data_labels: true,
    };
    const dss = buildChartJSConfig(spec, STUB_THEME as any).data.datasets as any[];
    expect(shown(dss[dss.length - 1].datalabels.display)).toBe(true);
    expect(dss[dss.length - 1].datalabels.formatter(null, { dataIndex: 0 })).toContain("7");
  });

  test("★瀑布：比率列不给 Σ 数字（把退款率累加是编数字）", () => {
    const spec: ChartSpec = {
      kind: "waterfall", title: "退款率桥",
      columns: [
        { key: "item", header: "环节", type: "categorical" },
        { key: "d", header: "退款率", type: "numeric", percent_scale: "fraction" },
      ],
      rows: [{ item: "A", d: 4.1 }, { item: "B", d: 2.8 }],
      row_count: 2,
      show_data_labels: true,
    };
    const dl = (buildChartJSConfig(spec, STUB_THEME as any).data.datasets[0] as any).datalabels;
    expect(shown(dl.display({ dataIndex: 0 }))).toBe(true);   // Label components as usual.
    expect(dl.display({ dataIndex: 2 })).toBe(false);  // Do not label Σ.
  });

  test("★瀑布 Σ：负总额不得丢负号（原来标成正数）", () => {
    const spec: ChartSpec = {
      kind: "waterfall", title: "负总额",
      columns: [
        { key: "item", header: "项", type: "categorical" },
        { key: "d", header: "增量", type: "numeric", unit: "万" },
      ],
      rows: [{ item: "A", d: 200 }, { item: "B", d: -700 }],
      row_count: 2,
      show_data_labels: true,
    };
    const f = (buildChartJSConfig(spec, STUB_THEME as any).data.datasets[0] as any).datalabels.formatter;
    // Cumulative total = -500; label must preserve minus sign (original implementation showed 500).
    expect(f(null, { dataIndex: 2 })).toMatch(/-|−/);
    expect(f(null, { dataIndex: 2 })).toContain("500");
  });
});

describe("R2-5 scatter 主角按组名映射（index 会错位）", () => {
  test("★含全空值分组时主角仍落在图注指的那组", () => {
    const spec: ChartSpec = {
      kind: "scatter", title: "分组",
      subtitle: "华南增长最快",
      columns: [
        { key: "g", header: "区域", type: "categorical" },
        { key: "x", header: "客流", type: "numeric" },
        { key: "y", header: "成交", type: "numeric" },
      ],
      // Entire 华东 group has null x → skipped during dataset creation, shifting later indices forward.
      rows: [
        { g: "华东", x: null, y: 1 },
        { g: "华南", x: 2, y: 3 },
        { g: "华东", x: null, y: 5 },
      ],
      row_count: 3,
    };
    const dss = buildChartJSConfig(spec, STUB_THEME as any).data.datasets as any[];
    const lead = dss.find((d) => d.pointRadius === 6);
    expect(lead?.label).toBe("华南");
  });
});


describe("review R2: 主角层集成守卫（此前 resolvePresentation 恒 null 也全绿）", () => {
  const leadSpec: ChartSpec = {
    kind: "bar", title: "分品类",
    subtitle: "女装一类贡献超三分之一",
    columns: [
      { key: "cat", header: "品类", type: "categorical" },
      { key: "v", header: "销售额", type: "numeric" },
    ],
    rows: [{ cat: "女装", v: 340 }, { cat: "男装", v: 210 }, { cat: "童装", v: 180 }],
    row_count: 3,
  };

  test("★图注命中 → 主角柱满色、其余退浅（scriptable backgroundColor 真被调用）", () => {
    const cfg = buildChartJSConfig(leadSpec, STUB_THEME as any);
    const bg = (cfg.data.datasets[0] as any).backgroundColor;
    expect(typeof bg).toBe("function"); // Use scriptable only when lead exists.
    const leadColor = bg({ dataIndex: 0 });
    const otherColor = bg({ dataIndex: 1 });
    expect(leadColor).toBe(STUB_THEME.palette[0]);
    expect(otherColor).not.toBe(leadColor);
    expect(otherColor).toMatch(/^#[0-9a-f]{6}$/); // mutedOf actually produced a color.
  });

  test("★无图注命中 → 全满色（fail-open，不是 scriptable）", () => {
    const cfg = buildChartJSConfig({ ...leadSpec, subtitle: "整体平稳" }, STUB_THEME as any);
    expect(typeof (cfg.data.datasets[0] as any).backgroundColor).toBe("string");
  });

  test("★折线主角：线宽 2.5 满色，非主角 1.5 退浅", () => {
    const spec: ChartSpec = {
      kind: "line", title: "趋势",
      subtitle: "营业额一路走低",
      columns: [
        { key: "m", header: "月份", type: "date" },
        { key: "rev", header: "营业额", type: "numeric" },
        { key: "profit", header: "利润", type: "numeric" },
      ],
      rows: [{ m: "2026-01", rev: 19.9, profit: 3.1 }, { m: "2026-02", rev: 17.7, profit: 2.9 }],
      row_count: 2,
    };
    const dss = buildChartJSConfig(spec, STUB_THEME as any).data.datasets as any[];
    expect(dss[0].borderWidth).toBe(2.5);
    expect(dss[1].borderWidth).toBe(1.5);
    expect(dss[1].borderColor).not.toBe(dss[0].borderColor);
  });
});

// ─────────────────────────────────────────────────────────────
// R7: color composition for production shape (CSS-compressed `--c-surface: #fff`).
// ─────────────────────────────────────────────────────────────

describe("★R7 生产形态主题：颜色合成不许产出非法串", () => {
  const VALID_HEX = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;

  const labelSpec: ChartSpec = {
    kind: "line", title: "月度营业额趋势",
    columns: [
      { key: "m", header: "月份", type: "categorical" },
      { key: "v", header: "营业额", type: "numeric" },
    ],
    rows: [{ m: "1月", v: 14.2 }, { m: "2月", v: 12.8 }, { m: "3月", v: 15.3 }],
    row_count: 3,
    show_data_labels: true,
  };

  // Since Chart Embed Spec D1 only labels drawn **inside** marks (pie slices) keep a backing plate; the colour rule
  // still applies there.
  const pieLabelSpec: ChartSpec = { ...labelSpec, kind: "pie" };

  test("★数值标签底片必须是合法颜色（旧写法在这里产出 `#fffeb`，canvas 静默落黑）", () => {
    const cfg = buildChartJSConfig(pieLabelSpec, PROD_SHAPE_THEME as any);
    const bg = (cfg.data.datasets[0] as any).datalabels.backgroundColor;
    expect(typeof bg).toBe("string");
    expect(bg, `底片颜色非法会让 canvas 保留上一次 fillStyle（黑块）：${bg}`).toMatch(VALID_HEX);
  });

  test("★同一份 spec 在 6 位与 3 位 surface 下，底片颜色必须等价", () => {
    const a = (buildChartJSConfig(pieLabelSpec, { ...STUB_THEME, surface: "#ffffff" } as any)
      .data.datasets[0] as any).datalabels.backgroundColor;
    const b = (buildChartJSConfig(pieLabelSpec, PROD_SHAPE_THEME as any)
      .data.datasets[0] as any).datalabels.backgroundColor;
    expect(b).toBe(a);
  });

  test("D1: labels outside marks carry no plate, sans 12 / 500, and zero padding (no hidden default 4)", () => {
    const dl = (buildChartJSConfig(labelSpec, PROD_SHAPE_THEME as any).data.datasets[0] as any).datalabels;
    expect(dl.backgroundColor).toBeUndefined();
    expect(dl.padding).toEqual({ top: 0, bottom: 0, left: 0, right: 0 });
    const font = typeof dl.font === "function" ? dl.font({ dataIndex: 0, datasetIndex: 0 }) : dl.font;
    expect(font).toMatchObject({ size: 12, weight: 500 });
    expect(font.family).not.toMatch(/mono/i);
  });
});

// ─────────────────────────────────────────────────────────────
// ★R8 axis ticks: last surface lacking column-level percentage scaling (caught on device 116, 1.0.97).
//
// Same chart: tooltip 100.0%, data label 29.2%, table 29.2%, but **axis ticks** alone show
// 0 / 0.1 / 0.2 / 0.3, with axis title "退款率（%）" ⇒ readers interpret 0.3%, actual value 29.2%.
// Use **real fraction-scale data** (refund rates persist as decimals such as 0.292); points
// (13.5 means 13.5%) never takes this scaling branch, making points input unable to exercise the guard.
// ─────────────────────────────────────────────────────────────
describe("★R8 轴刻度必须与标签/tooltip/表格同口径", () => {
  /** Actual shape of 116 `s_2bd4634c99ba` report 头部品牌退款率对比. */
  const fractionSpec = (kind: ChartSpec["kind"]): ChartSpec => ({
    kind, title: "头部品牌退款率对比",
    columns: [
      { key: "brand", header: "品牌", type: "categorical" },
      { key: "rate", header: "退款率", type: "numeric", percent_scale: "fraction" },
    ],
    rows: [
      { brand: "金辉福", rate: 0.126 },
      { brand: "老庙", rate: 0.292 },
      { brand: "珂莱蒂尔", rate: 0.261 },
    ],
    row_count: 3,
  });

  const tickOf = (cfg: any, axis: string) => cfg.options.scales[axis].ticks.callback;

  test("★bar/hbar/line：fraction 档刻度 ×100，0.3 不再冒充 0.3%", () => {
    for (const [kind, axis] of [["bar", "y"], ["hbar", "x"], ["line", "y"]] as const) {
      const cfg = buildChartJSConfig(fractionSpec(kind), STUB_THEME as any) as any;
      const cb = tickOf(cfg, axis);
      expect(`${kind}:${cb(0.3)}`).toBe(`${kind}:30`);
      expect(`${kind}:${cb(0.292)}`).toBe(`${kind}:29.2`);
      // Axis title supplies dimension; ticks do not repeat %.
      expect(cb(0.3)).not.toContain("%");
      expect(cfg.options.scales[axis].title.text).toBe("%");
    }
  });

  test("★points 档零改动（13.5 已经表示 13.5%，再 ×100 就是新的 100 倍错）", () => {
    const spec = fractionSpec("bar");
    spec.columns[1]!.percent_scale = "points";
    spec.rows = [{ brand: "a", rate: 17.44 }, { brand: "b", rate: 13.5 }];
    const cb = tickOf(buildChartJSConfig(spec, STUB_THEME as any) as any, "y");
    expect(cb(13.5)).toBe("13.5");
    expect(cb(20)).toBe("20");
  });

  test("★非百分比列零改动（营业额刻度还是紧凑档）", () => {
    const spec = fractionSpec("bar");
    spec.columns[1] = { key: "rate", header: "营业额", type: "numeric" };
    spec.rows = [{ brand: "a", rate: 0.3 }, { brand: "b", rate: 0.9 }];
    const cb = tickOf(buildChartJSConfig(spec, STUB_THEME as any) as any, "y");
    expect(cb(0.3)).toBe("0.3");
  });

  test("★dual_axis 两条轴各按自己那列定档（曾把「亿」和「%」混着算）", () => {
    const spec: ChartSpec = {
      kind: "dual_axis", title: "营业额与退款率",
      columns: [
        { key: "m", header: "月份", type: "categorical" },
        { key: "amt", header: "营业额", type: "numeric" },
        { key: "rate", header: "退款率", type: "numeric", percent_scale: "fraction" },
      ],
      rows: [{ m: "1月", amt: 1.98e9, rate: 0.292 }, { m: "2月", amt: 1.77e9, rate: 0.126 }],
      row_count: 2,
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any) as any;
    expect(tickOf(cfg, "y")(1.98e9)).not.toBe("198000000000");
    expect(tickOf(cfg, "y1")(0.292)).toBe("29.2");
  });

  test("★scatter 两条轴也接（此前是唯一绕开 axisTitleOf 的图种）", () => {
    const spec: ChartSpec = {
      kind: "scatter", title: "退款率 vs 退货率",
      columns: [
        { key: "shop", header: "门店", type: "categorical" },
        { key: "rate", header: "退款率", type: "numeric", percent_scale: "fraction" },
        { key: "back", header: "退货率", type: "numeric", percent_scale: "fraction" },
      ],
      rows: [{ shop: "a", rate: 0.292, back: 0.11 }, { shop: "b", rate: 0.126, back: 0.34 }],
      row_count: 2,
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any) as any;
    expect(tickOf(cfg, "x")(0.292)).toBe("29.2");
    expect(tickOf(cfg, "y")(0.34)).toBe("34");
    expect(cfg.options.scales.x.title.text).toBe("退款率（%）");
  });

  test("★刻度与同图数值标签逐字一致（这才是这条 bug 的判据）", () => {
    const cfg = buildChartJSConfig(
      { ...fractionSpec("bar"), show_data_labels: true }, STUB_THEME as any,
    ) as any;
    const label = cfg.data.datasets[0].datalabels.formatter(0.292);
    const tick = tickOf(cfg, "y")(0.292);
    expect(label).toBe("29.2%");
    expect(label).toBe(`${tick}%`);
  });
});


describe("散点的分类列是「行标识」时不再分组（116 语料 16/16 都是这种）", () => {
  function identitySpec(n: number, subtitle?: string): ChartSpec {
    return {
      kind: "scatter",
      title: "门店面积 vs 营业额",
      subtitle,
      columns: [
        { key: "门店", header: "门店", type: "categorical" },
        { key: "面积", header: "门店面积", type: "numeric" },
        { key: "GMV", header: "营业额", type: "numeric", unit: "元" },
      ],
      rows: Array.from({ length: n }, (_, i) => ({
        门店: `店${String(i).padStart(2, "0")}`,
        面积: 1000 + i * 600,
        GMV: 5e6 + i * 3e6,
      })),
      row_count: n,
    };
  }

  test("★★ 超过色板槽位就收成单系列——否则图例在用 4 种颜色声称区分 20 个门店", () => {
    const dss = buildChartJSConfig(identitySpec(20), STUB_THEME as any).data.datasets as any[];
    expect(dss).toHaveLength(1);
    expect(dss[0]._pointLabels).toHaveLength(20);
    expect(dss[0]._pointLabels[0]).toBe("店00");
  });

  test("★ 收成单系列之后身份不能丢：tooltip 逐点取名字，不是整条都写度量名", () => {
    const cfg = buildChartJSConfig(identitySpec(20), STUB_THEME as any);
    const ds = cfg.data.datasets[0] as any;
    const cb = (cfg.options as any).plugins.tooltip.callbacks.label;
    const line = cb({ dataset: ds, dataIndex: 3, parsed: { x: 1, y: 14e6 }, chart: { options: {} } });
    expect(line).toContain("店03");
    expect(line).not.toMatch(/^营业额:/);
  });

  test("★ 主角仍然高亮，只是从「一簇」变成「一个点」", () => {
    // ⚠ Contract changed once in batch 1 (per-point labels): buildScatter still supplies **point styles** directly,
    //   but plugin now decides **which points get labels** (collision avoidance needs real pixels unavailable to the pure function).
    //   Pin only styles here; label behavior lives in the "逐点标签" suite below.
    const cfg = buildChartJSConfig(identitySpec(20, "店07 面积不大却跑赢同侪"), STUB_THEME as any);
    const ds = cfg.data.datasets[0] as any;
    expect(Array.isArray(ds.pointRadius)).toBe(true);
    expect(ds.pointRadius[7]).toBe(6);
    expect(ds.pointRadius[0]).toBe(4); // Other points become small hollow ones.
    expect(ds.backgroundColor[7]).toBe(STUB_THEME.palette[0]);
    expect(ds.backgroundColor[0]).toBe(STUB_THEME.surface);
    // Label naming still follows per-point identity (formatter now uses dataIndex).
    expect(ds.datalabels.formatter(null, { dataIndex: 7 })).toBe("店07");
    expect(ds.datalabels.formatter(null, { dataIndex: 0 })).toBe("店00");
  });

  test("★ 反向钉死：分组数在色板槽位以内时照旧分组（真·分组散点没被这条改动波及）", () => {
    const spec: ChartSpec = {
      kind: "scatter",
      title: "按区域",
      columns: [
        { key: "g", header: "区域", type: "categorical" },
        { key: "x", header: "客流", type: "numeric" },
        { key: "y", header: "成交", type: "numeric" },
      ],
      rows: [
        { g: "华东", x: 1, y: 2 },
        { g: "华东", x: 3, y: 4 },
        { g: "华南", x: 5, y: 6 },
        { g: "华北", x: 7, y: 8 },
      ],
      row_count: 4,
    };
    const dss = buildChartJSConfig(spec, STUB_THEME as any).data.datasets as any[];
    expect(dss.map((d) => d.label)).toEqual(["华东", "华南", "华北"]);
    expect(dss[0]._pointLabels).toBeUndefined();
  });

  test("★ 单系列时不许有重复用色：所有点同一个色板槽位", () => {
    const dss = buildChartJSConfig(identitySpec(20), STUB_THEME as any).data.datasets as any[];
    const borders = new Set(dss[0].borderColor as string[]);
    expect(borders.size).toBe(1);
    expect([...borders][0]).toBe(STUB_THEME.palette[0]);
  });
});

describe("x 轴列：列序选中的那个分不出行时，改选能标识行的（116 语料 3 张）", () => {
  test("★★ [省(5), 市(10)] 10 行 —— 10 根柱不能只有 5 个标签", () => {
    const rows = [
      { 省: "上海", 市: "静安区", v: 149 }, { 省: "上海", 市: "浦东新区", v: 131 },
      { 省: "北京", 市: "朝阳区", v: 128 }, { 省: "北京", 市: "海淀区", v: 121 },
      { 省: "广东", 市: "天河区", v: 118 }, { 省: "广东", 市: "福田区", v: 110 },
      { 省: "江苏", 市: "鼓楼区", v: 101 }, { 省: "江苏", 市: "玄武区", v: 98 },
      { 省: "浙江", 市: "西湖区", v: 92 }, { 省: "浙江", 市: "拱墅区", v: 88 },
    ];
    const cfg = buildChartJSConfig(
      {
        kind: "bar", title: "2026年省市营业额排名 Top10",
        columns: [
          { key: "省", header: "省", type: "geo" },
          { key: "市", header: "市", type: "geo" },
          { key: "v", header: "营业额", type: "numeric" },
        ],
        rows, row_count: rows.length,
      },
      STUB_THEME as any,
    );
    expect(cfg.data.labels).toEqual(rows.map((r) => r.市));
  });

  test("★ 列序第一个已经能标识行时不动（`review[2]` 的列序约定原样保留）", () => {
    const rows = [
      { 分行: "海南分行", 月份: "1月", v: 1 },
      { 分行: "广东分行", 月份: "2月", v: 2 },
      { 分行: "江苏分行", 月份: "3月", v: 3 },
    ];
    const cfg = buildChartJSConfig(
      {
        kind: "bar", title: "分行销量",
        columns: [
          { key: "分行", header: "分行", type: "categorical" },
          { key: "月份", header: "月份", type: "date" },
          { key: "v", header: "销量", type: "numeric" },
        ],
        rows, row_count: rows.length,
      },
      STUB_THEME as any,
    );
    expect(cfg.data.labels).toEqual(["海南分行", "广东分行", "江苏分行"]);
  });

  test("★★ 长表恒不生效：没有任何单列能标识每一行，交给 pivot 而不是换 x 轴", () => {
    const rows: Array<Record<string, string | number>> = [];
    for (const cat of ["女装", "鞋包", "美妆"]) {
      for (const m of ["1月", "2月", "3月", "4月"]) rows.push({ 品类: cat, 月份: m, v: 10 });
    }
    const cfg = buildChartJSConfig(
      {
        kind: "bar", title: "长表",
        columns: [
          { key: "品类", header: "品类", type: "categorical" },
          { key: "月份", header: "月份", type: "categorical" },
          { key: "v", header: "营业额", type: "numeric" },
        ],
        rows, row_count: rows.length,
      },
      STUB_THEME as any,
    );
    // pivotLongSpec handles it ⇒ categories on x, months as series (12 rows were not replaced by selecting another x column).
    expect(cfg.data.labels).toEqual(["女装", "鞋包", "美妆"]);
    expect(cfg.data.datasets).toHaveLength(4);
  });

  test("★★ date 列一律不动：类目列碰巧逐行唯一也不许抢走时间轴", () => {
    // ⚠ Two numeric columns: with one, `pivotLongSpec` handles it first (also correct);
    //   this case checks that **when it cannot**, `preferIdentifyingDim` must not replace date x.
    const rows = [
      { 每月: "2026年1月", 门店: "东湖街", v: 1, w: 10 },
      { 每月: "2026年1月", 门店: "杏林", v: 2, w: 20 },
      { 每月: "2026年2月", 门店: "新纪城", v: 3, w: 30 },
      { 每月: "2026年2月", 门店: "中关村", v: 4, w: 40 },
      { 每月: "2026年3月", 门店: "双桥", v: 5, w: 50 },
    ];
    const cfg = buildChartJSConfig(
      {
        kind: "line", title: "走势",
        columns: [
          { key: "每月", header: "每月", type: "date" },
          { key: "门店", header: "门店", type: "categorical" },
          { key: "v", header: "营业额", type: "numeric" },
          { key: "w", header: "客流量", type: "numeric" },
        ],
        rows, row_count: rows.length,
      },
      STUB_THEME as any,
    );
    expect(cfg.data.labels).toEqual(rows.map((r) => r.每月));
  });
});

describe("x 轴换列的两道闸（都是全量对拍逼出来的，第一版没有、当场制造回退）", () => {
  test("★★ 列序选中的那列本来就分得出行时不让位（11/15 个值，够用了）", () => {
    // `dual_axis | Top15 城市月坪效排名`: `市` has 11 values; `门店面积` has 15.
    // Initial criterion required only another row-unique column, immediately switching x from city names to area numbers.
    const rows = Array.from({ length: 15 }, (_, i) => ({
      市: ["静安区", "河北区", "南京市", "昌平区", "朝阳区", "徐汇区", "天河区", "福田区", "鼓楼区", "西湖区", "拱墅区"][i % 11]!,
      店: `店${i}`,
      v: 100 + i,
    }));
    const cfg = buildChartJSConfig(
      {
        kind: "bar", title: "Top15 城市月坪效排名",
        columns: [
          { key: "市", header: "市", type: "geo" },
          { key: "店", header: "门店", type: "categorical" },
          { key: "v", header: "月坪效", type: "numeric" },
        ],
        rows, row_count: rows.length,
      },
      STUB_THEME as any,
    );
    expect(cfg.data.labels).toEqual(rows.map((r) => r.市));
  });

  test("★★ 整列都是「数字长相」的不算行的名字（`24,303` 不是门店名）", () => {
    // `bar | 门店面积与营业额关系`: `门店面积` was categorical, with values like `24,303`;
    // unique across 39 rows ⇒ first version replaced `省`, producing area numbers on x axis.
    // Server parsing already restores such columns, but **existing specs rerender every time they open**.
    const rows = Array.from({ length: 10 }, (_, i) => ({
      省: ["上海", "北京", "天津", "江苏", "浙江"][i % 5]!,
      面积: `${1 + i},${String(100 + i).padStart(3, "0")}`,
      v: 100 + i,
    }));
    const cfg = buildChartJSConfig(
      {
        kind: "bar", title: "门店面积与营业额关系",
        columns: [
          { key: "省", header: "省", type: "geo" },
          { key: "面积", header: "门店面积", type: "categorical" },
          { key: "v", header: "营业额", type: "numeric" },
        ],
        rows, row_count: rows.length,
      },
      STUB_THEME as any,
    );
    expect(cfg.data.labels).toEqual(rows.map((r) => r.省));
  });

  test("★★ 116 真例：整列常量的药品名不该当 x 轴，20 根柱本该是 20 家医院", () => {
    const rows = Array.from({ length: 20 }, (_, i) => ({
      药品: "注射用示例药品甲",
      医院: `第 ${i + 1} 医院`,
      v: 200 - i,
    }));
    const cfg = buildChartJSConfig(
      {
        kind: "hbar", title: "示例药品甲 Top 20 医院",
        columns: [
          { key: "药品", header: "药品", type: "categorical" },
          { key: "医院", header: "医院", type: "categorical" },
          { key: "v", header: "销售额", type: "numeric" },
        ],
        rows, row_count: rows.length,
      },
      STUB_THEME as any,
    );
    expect(cfg.data.labels).toEqual(rows.map((r) => r.医院));
  });
});

describe("chart-style renderer geometry", () => {
  test("projects line geometry and grid density without changing ChartSpec", () => {
    const spec = _spec("line");
    const before = structuredClone(spec);
    const cfg = buildChartJSConfig(spec, {
      ...STUB_THEME,
      gridSoft: "#D5D7DA",
      gridDensity: "strong",
      lineWidth: 3,
      pointRadius: 4,
      barRadius: 2,
    } as any);

    expect((cfg.data.datasets[0] as any).borderWidth).toBe(3);
    expect((cfg.data.datasets[0] as any).pointRadius).toBe(4);
    expect((cfg.options as any).scales.y.grid).toEqual({
      color: "#E5E2DA",
      borderDash: [],
    });
    expect(spec).toEqual(before);
  });

  // Review: declaring the waterfall's value axis (for label headroom) let the presentation restyle its grid. Labelled
  // and unlabelled waterfalls must get the same grid — the chart style's, like every other bar chart.
  test("waterfalls follow the chart style's gridlines whether or not they carry labels", () => {
    const theme = { ...STUB_THEME, gridSoft: "#D5D7DA", gridDensity: "strong", palette: STUB_THEME.palette } as any;
    const wf = (show: boolean): ChartSpec => ({
      kind: "waterfall", title: "桥", row_count: 2, show_data_labels: show,
      columns: [
        { key: "i", header: "项", type: "categorical" },
        { key: "d", header: "增量", type: "numeric" },
      ],
      rows: [{ i: "A", d: 3 }, { i: "B", d: -1 }],
    });
    const labelled = (buildChartJSConfig(wf(true), theme).options as any).scales.y;
    const bare = (buildChartJSConfig(wf(false), theme).options as any).scales.y;
    expect(bare.grid).toEqual({ color: "#E5E2DA", borderDash: [] });
    expect(labelled.grid).toEqual(bare.grid);
  });
});

describe("逐点标签：插件把避让结果接回 datalabels（批1 的接线）", () => {
  function identity40(subtitle?: string): ChartSpec {
    return {
      kind: "scatter",
      title: "面积 vs 营业额",
      subtitle,
      columns: [
        { key: "门店", header: "门店", type: "categorical" },
        { key: "面积", header: "面积", type: "numeric" },
        { key: "GMV", header: "营业额", type: "numeric", unit: "元" },
      ],
      rows: Array.from({ length: 40 }, (_, i) => ({
        门店: `门店${String(i).padStart(2, "0")}`,
        面积: 1000 + (i % 20) * 1200,
        GMV: 5e7 + ((i * 7) % 30) * 3e7,
      })),
      row_count: 40,
    };
  }

  /** Create a sufficient fake chart and execute plugin afterLayout once. */
  function runPlugin(cfg: ReturnType<typeof buildChartJSConfig>, plugin?: any) {
    const p = plugin ?? ((cfg.plugins ?? [])[0] as any);
    const ds = cfg.data.datasets[0] as any;
    const chart = {
      scales: {
        x: { getPixelForValue: (v: number) => (v / 30000) * 600 },
        y: { getPixelForValue: (v: number) => 300 - (v / 1e9) * 300 },
      },
      data: cfg.data,
      ctx: { save() {}, restore() {}, measureText: (s: string) => ({ width: s.length * 6 }), font: "" },
      chartArea: { top: 0, bottom: 300, left: 0, right: 600 },
    };
    p.afterLayout(chart);
    return ds;
  }

  /** datalabels predicates receive dataset in ctx; fake chart must supply it too,
   *  or the test uses a call shape that never exists in reality. */
  const shown = (ds: any) =>
    Array.from({ length: (ds.data ?? []).length }, (_, i) =>
      ds.datalabels.display({ dataIndex: i, datasetIndex: 0, dataset: ds }),
    ).filter(Boolean).length;

  test("★★ 跑完插件后，标出来的点从 0 变成一批", () => {
    const cfg = buildChartJSConfig(identity40(), STUB_THEME as any);
    const ds = cfg.data.datasets[0] as any;
    // Empty before plugin runs: pure function lacks pixels and must wait for afterLayout.
    expect(ds.datalabels.display({ dataIndex: 0, dataset: ds })).toBe(false);
    runPlugin(cfg);
    expect(shown(ds)).toBeGreaterThan(5);
  });

  test("★★ 重渲之后仍然生效——旧插件 + 新 dataset 也要对上", () => {
    // Pin actual bug reproduced locally and on 116: `buildChartJSConfig` reruns every render,
    // creating a new dataset, while react-chartjs-2 reads `plugins` **only during construction**.
    // First version kept collision results in a closure-captured object ⇒ plugin wrote old object, predicates read new one;
    // labels never appeared (plugin reported picked=18, live dataset display values all false).
    // Results now live on dataset; plugin afterLayout reads `chart.data.datasets[0]`,
    // writing to whichever object is live.
    const first = buildChartJSConfig(identity40(), STUB_THEME as any);
    const oldPlugin = (first.plugins ?? [])[0] as any;
    const second = buildChartJSConfig(identity40(), STUB_THEME as any);
    const ds2 = runPlugin(second, oldPlugin); // Old plugins + new config
    expect(shown(ds2)).toBeGreaterThan(5);
  });

  // Readability audit (33 scatters): the plugin must hand every drawn marker to the picker, or labels avoid each
  // other but still land on unlabelled neighbours.
  function coveredPoints(ds: any): number {
    const px = (p: { x: number; y: number }) => ({ px: (p.x / 30000) * 600, py: 300 - (p.y / 1e9) * 300 });
    const radii = ds.pointRadius as number[];
    let covered = 0;
    for (const i of ds._labelPicked as Set<number>) {
      const p = px(ds.data[i]);
      const half = (ds._pointLabels[i].length * 6 + 6) / 2;
      const box = labelBox(p.px, p.py, 2 * half, ds._labelAlign.get(i), 14, 6);
      ds.data.forEach((q: { x: number; y: number }, j: number) => {
        if (j === i) return;
        const m = px(q);
        const cx = Math.max(box.l, Math.min(m.px, box.r));
        const cy = Math.max(box.t, Math.min(m.py, box.b));
        if ((m.px - cx) ** 2 + (m.py - cy) ** 2 < radii[j]! ** 2) covered += 1;
      });
    }
    return covered;
  }

  test("no picked label covers another drawn point", () => {
    const ds = runPlugin(buildChartJSConfig(identity40(), STUB_THEME as any));
    expect((ds._labelPicked as Set<number>).size).toBeGreaterThan(5);
    expect(coveredPoints(ds)).toBe(0);
  });

  test("a label whose upper slot holds a neighbour flips below it", () => {
    // B sits 6px above A: A's label in the top slot would lie on B's marker.
    const spec: ChartSpec = {
      ...identity40(),
      rows: [
        { 门店: "A店", 面积: 15000, GMV: 5e8 },
        { 门店: "B店", 面积: 15000, GMV: 5.2e8 },
        // Far-away points on a high line: per-point identity labels need more stores than palette colors (STUB_THEME
        // has 4), and the line puts A furthest from the trend, so A is placed first and its top slot is still free of
        // labels — only B's marker is in the way.
        { 门店: "C店", 面积: 3000, GMV: 9e8 },
        { 门店: "D店", 面积: 10000, GMV: 9e8 },
        { 门店: "E店", 面积: 20000, GMV: 9e8 },
        { 门店: "F店", 面积: 27000, GMV: 9e8 },
      ],
      row_count: 6,
    };
    const ds = runPlugin(buildChartJSConfig(spec, STUB_THEME as any));
    expect(ds._labelPicked.has(0)).toBe(true);
    expect(ds._labelAlign.get(0)).toBe("bottom");
    expect(coveredPoints(ds)).toBe(0);
  });

  test("★ 插件确实被挂进 config（不然上面那条永远跑不到真机）", () => {
    const cfg = buildChartJSConfig(identity40(), STUB_THEME as any);
    // ⚠ Check by **id**, not array length: the `buildChartJSConfig` exit also attaches
    // `edge-label-padding` universally; a hard-coded length would fail whenever another plugin is added.
    expect(pluginIds(cfg)).toContain("scatter-point-labels");
  });

  test("★ 主角在图注里点名时一定被标", () => {
    const cfg = buildChartJSConfig(identity40("门店37 面积不大却跑赢同侪"), STUB_THEME as any);
    const ds = runPlugin(cfg);
    expect(ds.datalabels.display({ dataIndex: 37, datasetIndex: 0, dataset: ds })).toBe(true);
  });

  test("★ align 是 scriptable 的——顶边的点要能翻到下方", () => {
    const cfg = buildChartJSConfig(identity40(), STUB_THEME as any);
    const ds = runPlugin(cfg);
    expect(typeof ds.datalabels.align).toBe("function");
    const dirs = Array.from({ length: 40 }, (_, i) => ds.datalabels.align({ dataIndex: i, dataset: ds }));
    expect(dirs.every((d) => ["top", "bottom", "right", "left"].includes(d))).toBe(true);
  });

  test("★★ 主角与其余用不同字号——插件按同一套字体量宽，两边必须真的不同", () => {
    // The first version made the lead predicate in `leadAwareLabelStyle` always true, enlarging every label to
    // the lead font (sans 12.5/650), while the plugin still measured with the smaller font ⇒ measured narrower than
    // drawn ⇒ avoidance failed and labels overlapped (`姚家园` covered `马家堡` on a real device).
    // Since D1 both use the interface sans: the protagonist differs by size and weight, not by family.
    const cfg = buildChartJSConfig(identity40("门店37 面积不大却跑赢同侪"), STUB_THEME as any);
    const ds = cfg.data.datasets[0] as any;
    const leadFont = ds.datalabels.font({ dataIndex: 37, datasetIndex: 0 });
    const restFont = ds.datalabels.font({ dataIndex: 0, datasetIndex: 0 });
    expect(leadFont.size).toBeGreaterThan(restFont.size);
    expect(leadFont.weight).toBeGreaterThan(restFont.weight);
    expect(leadFont.family).toBe(restFont.family);
  });

  test("★ 反向钉死：真·分组散点（≤色板槽位）不挂这个插件", () => {
    const spec: ChartSpec = {
      kind: "scatter",
      title: "按区域",
      columns: [
        { key: "g", header: "区域", type: "categorical" },
        { key: "x", header: "客流", type: "numeric" },
        { key: "y", header: "成交", type: "numeric" },
      ],
      rows: [
        { g: "华东", x: 1, y: 2 },
        { g: "华南", x: 3, y: 4 },
        { g: "华北", x: 5, y: 6 },
      ],
      row_count: 3,
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    expect(pluginIds(cfg)).not.toContain("scatter-point-labels");
  });
});

describe("批2：数值标签策略在生产上从未执行过（服务端恒把它按死）", () => {
  const dl = (cfg: ReturnType<typeof buildChartJSConfig>) =>
    (cfg.data.datasets[0] as any).datalabels;

  /** Single series, 20 categories: the soft guideline chooses "off", so any enabled case below
   *  must have actually executed a **lower rung of the ladder**, rather than passing on defaults. */
  const wide = () => _oneSeries(20);

  test("★★ 服务端不表态时梯子才活得成——显式 false 会一路短路到底", () => {
    // Production shape: `ChartSpec.show_data_labels` defaults to False + `_drop_empty` retains False
    // ⇒ every chart receives an explicit false, skipping always-on export/mobile behavior and the soft guideline.
    // On 116, all 2781/2781 persisted charts were false. The server default has been changed to None.
    const pinned = { ...wide(), show_data_labels: false };
    for (const opts of [{}, { surface: "export" as const }, { forceDataLabels: true }]) {
      expect(shown(dl(buildChartJSConfig(pinned, STUB_THEME as any, opts)).display)).toBe(false);
    }
    // With the same opts, export/mobile must enable labels when the field is absent (= the ladder actually descends).
    expect(shown(dl(buildChartJSConfig(wide(), STUB_THEME as any, { surface: "export" })).display)).toBe(true);
    expect(shown(dl(buildChartJSConfig(wide(), STUB_THEME as any, { forceDataLabels: true })).display)).toBe(true);
  });

  test("★「没有读数退路」看 renderContext 的真能力，不只看 surface 字面量", () => {
    const ctx = (over: Partial<ChartRenderContext>): ChartRenderContext => ({
      ...COMPACT_CONTEXT,
      bounds: { widthPx: 900, heightPx: 480, devicePixelRatio: 1 },
      input: { hover: true, pointer: "fine", keyboard: true },
      output: { medium: "screen", static: false },
      exactDataAlternative: { kind: "inspect-table" },
      ...over,
    });
    const on = (c: ChartRenderContext) =>
      shown(dl(buildChartJSConfig(wide(), PRESET_THEME as any, { renderContext: c })).display);

    // Rasterized output (Playwright screenshots for report PDF/PPTX) counts even when surface is not export.
    expect(on(ctx({ output: { medium: "raster", static: true } }))).toBe(true);
    // Chrome-free embedding: neither hover nor a "查看数据" table is available.
    expect(on(ctx({
      input: { hover: false, pointer: "coarse", keyboard: true },
      exactDataAlternative: { kind: "none" },
    }))).toBe(true);
    // ★ Negative control: losing just one does not count; hover still provides a way to read values.
    expect(on(ctx({ exactDataAlternative: { kind: "none" } }))).toBe(false);
    // ★ Negative control: no hover, but a "查看数据" table remains available; same reasoning.
    expect(on(ctx({ input: { hover: false, pointer: "coarse", keyboard: true } }))).toBe(false);
  });

  test("★★三态：非主角是 `\"auto\"`（重叠自隐），主角是 `true`（永不被隐藏）", () => {
    // datalabels' collide() hides only the hidable side, so this distinction **is**
    // "others yield to the lead when space runs out". Making it binary (all true) clutters dense charts;
    // reversing it (lead auto) makes the lead disappear first in dense charts.
    const cfg = buildChartJSConfig(
      { ...wide(), subtitle: "类0 贡献最大" },
      STUB_THEME as any,
      { surface: "export" },
    );
    const d = dl(cfg).display;
    expect(typeof d).toBe("function"); // Use a scriptable value only when a lead is present.
    expect(d({ dataIndex: 0 })).toBe(true);     // Lead
    expect(d({ dataIndex: 1 })).toBe("auto");   // Others
  });

  test("★ 关着的时候仍然是 `false`，不是 `\"auto\"`（auto 也是「画」）", () => {
    expect(dl(buildChartJSConfig(wide(), STUB_THEME as any)).display).toBe(false);
  });
});


describe("explicit fraction metadata stays consistent between tables and charts", () => {
  /** Synthetic rows explicitly carry fraction metadata, which takes precedence over name inference. */
  const real = (kind: string): ChartSpec => ({
    kind,
    title: "Sample representative attainment",
    columns: [
      { key: "代表姓名", header: "代表姓名", type: "categorical" },
      { key: "金额达成率", header: "金额达成率", type: "numeric", percent_scale: "fraction" },
    ],
    rows: [
      { 代表姓名: "Example A", 金额达成率: 1.28 },
      { 代表姓名: "Example B", 金额达成率: 1.16 },
      { 代表姓名: "Example C", 金额达成率: 1.11 },
    ],
    row_count: 3,
    show_data_labels: true,
  } as unknown as ChartSpec);

  test("★★ 数值标签必须是 128.0% 而不是 1.3%", () => {
    // Chart labels must pass column metadata to the same formatter used by tables.
    const f = (buildChartJSConfig(real("bar"), STUB_THEME as any).data.datasets[0] as any).datalabels.formatter;
    expect(f(1.28)).toBe("128%");
  });

  test("★ 轴刻度同档（轴此前也走同一个漏传的 ctx）", () => {
    const cfg = buildChartJSConfig(real("bar"), STUB_THEME as any);
    const tick = (cfg.options as any).scales.y.ticks.callback(1.28, 0, []);
    // fraction ⇒ ×100 before abbreviation; points would produce "1.3".
    expect(String(tick)).not.toBe("1.3");
    expect(String(tick)).toContain("128");
  });

  test("★★ 显式 percent_scale 是第 1 档：即使列名换成不认识的词也必须照办", () => {
    const spec = real("bar");
    spec.columns![1]!.header = "zzz";           // Name-family matching no longer applies.
    (spec.columns![1] as any).source_header = undefined;
    const f = (buildChartJSConfig(spec, STUB_THEME as any).data.datasets[0] as any).datalabels.formatter;
    expect(f(1.28)).toBe("128%");
  });

  test("★ 阴性对照：真·points 列不许被放大（否则这条修法就是把所有百分比都乘 100）", () => {
    const spec = real("bar");
    spec.columns![1]!.header = "市场份额";          // points family
    (spec.columns![1] as any).percent_scale = "points";
    (spec.columns![1] as any).format = "percent";   // Still a percentage column, just a different scale tier.
    const f = (buildChartJSConfig(spec, STUB_THEME as any).data.datasets[0] as any).datalabels.formatter;
    expect(f(1.28)).toBe("1.28%");
  });

  test("★ 阴性对照二：非百分比列一点不受影响", () => {
    const spec = real("bar");
    spec.columns![1]!.header = "营业额";
    (spec.columns![1] as any).percent_scale = undefined;
    const f = (buildChartJSConfig(spec, STUB_THEME as any).data.datasets[0] as any).datalabels.formatter;
    expect(f(1.28)).not.toContain("%");
  });
});

describe("点标签压住纵轴刻度文字时侧摆（116 手机：首点 19.9 盖住刻度 20）", () => {
  const H1: ChartSpec = {
    kind: "line",
    title: "2026年上半年（1-6月）月度营业额趋势",
    row_count: 6,
    columns: [
      { key: "month", header: "月份", type: "date" },
      { key: "revenue", header: "营业额(亿元)", type: "numeric" },
    ],
    rows: [19.9, 17.7, 14.1, 13.9, 13.0, 12.6].map((revenue, i) => ({ month: `2026-0${i + 1}`, revenue })),
  };

  /**
   * A sufficient fake chart: plot area x 40–290; the y axis maps 10–20 to 300–10.
   * Tick text is right-aligned at x=36 (left axis) or left-aligned at x=294 (right axis), with 12px line height.
   */
  function fakeChart(
    cfg: ReturnType<typeof buildChartJSConfig>,
    { tickRight = false, tickValues = [20, 15, 10] }: { tickRight?: boolean; tickValues?: number[] } = {},
  ) {
    const yPixel = (v: number) => 300 - ((v - 10) / 10) * 290;
    const x = { axis: "x", getPixelForValue: (i: number) => 40 + i * 50 };
    const y = {
      axis: "y",
      position: tickRight ? "right" : "left",
      options: { display: true, ticks: { display: true } },
      getPixelForValue: yPixel,
      getLabelItems: () =>
        tickValues.map((v) => ({
          label: String(v),
          font: { string: "11px mono", lineHeight: 12 },
          textOffset: 0,
          options: {
            translation: [tickRight ? 294 : 36, yPixel(v)],
            textAlign: tickRight ? "left" : "right",
            textBaseline: "middle",
          },
        })),
    };
    const datasets = cfg.data.datasets as any[];
    return {
      options: {},
      data: cfg.data,
      scales: { x, y },
      ctx: { save() {}, restore() {}, measureText: (s: string) => ({ width: s.length * 6 }), font: "" },
      isDatasetVisible: () => true,
      getDatasetMeta: (i: number) => ({
        iScale: x,
        vScale: y,
        _stacked: false,
        controller: { getParsed: (j: number) => ({ x: j, y: datasets[i].data[j] }) },
      }),
    };
  }

  const tickAvoid = (cfg: ReturnType<typeof buildChartJSConfig>) =>
    (cfg.plugins ?? []).find((p) => p.id === "tick-label-avoid") as any;
  const alignAt = (ds: any, dataIndex: number) =>
    typeof ds.datalabels.align === "function"
      ? ds.datalabels.align({ dataIndex, datasetIndex: 0, dataset: ds })
      : ds.datalabels.align;

  test("★★ 顶边留白首帧就生效：锚点从 scale 现算，不等元素有坐标（导出页构造后不再 update）", () => {
    const cfg = buildChartJSConfig(H1, STUB_THEME as any, { surface: "export" });
    const edgePad = (cfg.plugins ?? []).find((p) => p.id === "edge-label-padding") as any;
    const base = fakeChart(cfg);
    let updates = 0;
    // First frame: elements have no coordinates yet (meta.data is empty); only scales and parsed values exist.
    const chart = {
      ...base,
      width: 330,
      chartArea: { top: 10, bottom: 300, left: 40, right: 290 },
      getDatasetMeta: (i: number) => ({ ...base.getDatasetMeta(i), data: [] }),
      update: () => { updates += 1; },
    };
    edgePad.afterLayout(chart);
    // The 19.9 anchor is at y≈12.9; the upward label is 12×1.2+2+4≈20.4 high ⇒ extends about 7.5px above the canvas.
    // Chart.js keeps the larger of padding and the axis's tick overhang (plot top 10 here), so the padding must be the
    // current plot top plus the overflow: 10 + 9 = 19 (a real Chart instance is checked in bar-label-geometry.test.ts).
    expect((chart.options as any).layout?.padding?.top).toBe(19);
    expect(updates).toBe(1);
  });

  test("right edge: the last label is measured at its position in this layout, not the stale previous frame", () => {
    const cfg = buildChartJSConfig(H1, STUB_THEME as any, { surface: "export" });
    const edgePad = (cfg.plugins ?? []).find((p) => p.id === "edge-label-padding") as any;
    const base = fakeChart(cfg);
    const ds = cfg.data.datasets[0] as any;
    const arg = { dataIndex: 5, datasetIndex: 0, dataset: ds };
    // Positive control: the last point really carries a label, otherwise no padding proves nothing.
    expect(ds.datalabels.display(arg)).toBeTruthy();
    const lastText = String(ds.datalabels.formatter(ds.data[5], arg));
    // A rerender reset layout.padding to 0; elements still sit where the previous, padded frame put them
    // (last at x=265), while this layout's scale puts the last point at x=290 on a 300px canvas.
    const chart = {
      ...base,
      width: 300,
      chartArea: { top: 30, bottom: 300, left: 40, right: 290 },
      getDatasetMeta: (i: number) => ({
        ...base.getDatasetMeta(i),
        data: Array.from({ length: 6 }, (_, j) => ({ x: 40 + j * 45 })),
      }),
      update: () => {},
    };
    edgePad.afterLayout(chart);
    // fakeChart measures 6px per character; labels add 6px horizontal padding; 10px natural gap on the right.
    const labelWidth = lastText.length * 6 + 6;
    expect((chart.options as any).layout?.padding?.right).toBe(Math.ceil(labelWidth / 2 - 10) + 1);
  });

  /** Width the plugin measures for the last label: fakeChart counts 6px per character, labels add 6px of padding. */
  function lastLabelWidth(cfg: ReturnType<typeof buildChartJSConfig>): number {
    const ds = cfg.data.datasets[0] as any;
    const arg = { dataIndex: 5, datasetIndex: 0, dataset: ds };
    const display = typeof ds.datalabels.display === "function" ? ds.datalabels.display(arg) : ds.datalabels.display;
    // Positive control: the last point really carries a label, otherwise no padding proves nothing.
    expect(display).toBeTruthy();
    return String(ds.datalabels.formatter(ds.data[5], arg)).length * 6 + 6;
  }
  const needFor = (width: number, gap: number) => Math.max(0, Math.ceil(width / 2 - gap) + 1);

  test("right edge on bars: the anchor comes from the bar controller's ruler, not the band centre or stale frame", () => {
    const cfg = buildChartJSConfig({ ...H1, kind: "bar" }, STUB_THEME as any, { surface: "export" });
    const edgePad = (cfg.plugins ?? []).find((p) => p.id === "edge-label-padding") as any;
    expect(edgePad).toBeDefined();
    const base = fakeChart(cfg);
    const width = lastLabelWidth(cfg);
    // Grouped bars sit off the band centre: the ruler puts the last bar at x=284, the index scale at 290,
    // and the stale previous frame at 265. Only the ruler position is right for this layout.
    const chart = {
      ...base,
      width: 300,
      chartArea: { top: 30, bottom: 300, left: 40, right: 290 },
      getDatasetMeta: (i: number) => {
        const meta = base.getDatasetMeta(i) as any;
        return {
          ...meta,
          data: Array.from({ length: 6 }, (_, j) => ({ x: 40 + j * 45 })),
          controller: {
            ...meta.controller,
            _getRuler: () => ({ stub: true }),
            _calculateBarIndexPixels: (j: number) => ({ center: 34 + j * 50 }),
          },
        };
      },
      update: () => {},
    };
    edgePad.afterLayout(chart);
    expect((chart.options as any).layout?.padding?.right ?? 0).toBe(needFor(width, 300 - 284));
  });

  test("first frame: without element coordinates the right gap is measured from chartArea.right", () => {
    const cfg = buildChartJSConfig(H1, STUB_THEME as any, { surface: "export" });
    const edgePad = (cfg.plugins ?? []).find((p) => p.id === "edge-label-padding") as any;
    const base = fakeChart(cfg);
    const width = lastLabelWidth(cfg);
    // Elements exist but have no x yet. The export page never updates again, so its pixels depend on this
    // fallback staying chartArea.right (280 here) rather than the index scale (290).
    const chart = {
      ...base,
      width: 300,
      chartArea: { top: 30, bottom: 300, left: 40, right: 280 },
      getDatasetMeta: (i: number) => ({ ...base.getDatasetMeta(i), data: Array.from({ length: 6 }, () => ({})) }),
      update: () => {},
    };
    edgePad.afterLayout(chart);
    expect((chart.options as any).layout?.padding?.right ?? 0).toBe(needFor(width, 300 - 280));
    expect(needFor(width, 300 - 280)).not.toBe(needFor(width, 300 - 290));
  });

  test("★★ 冲突的首点标签朝右上摆，其余标签仍在正上方", () => {
    const cfg = buildChartJSConfig(H1, STUB_THEME as any, { surface: "export" });
    const ds = cfg.data.datasets[0] as any;
    expect(shown(ds.datalabels.display({ dataIndex: 0 }))).toBe(true); // Positive control: the first point really has a label.
    tickAvoid(cfg).afterLayout(fakeChart(cfg));
    expect(alignAt(ds, 0)).toBe(-45);
    expect(alignAt(ds, 5)).toBe("top");
  });

  test("★★ 不冲突的图连配置都不碰（align 仍是原来的字面量）", () => {
    const cfg = buildChartJSConfig(H1, STUB_THEME as any, { surface: "export" });
    const ds = cfg.data.datasets[0] as any;
    // Only tick 10 remains, far from the first-point label (just above y≈13).
    tickAvoid(cfg).afterLayout(fakeChart(cfg, { tickValues: [10] }));
    expect(ds.datalabels.align).toBe("top");
    expect(ds._tickAvoidAlign).toBeUndefined();
  });

  test("★ 冲突消失（重排之后）时退回原朝向", () => {
    const cfg = buildChartJSConfig(H1, STUB_THEME as any, { surface: "export" });
    const ds = cfg.data.datasets[0] as any;
    const plugin = tickAvoid(cfg);
    plugin.afterLayout(fakeChart(cfg));
    expect(alignAt(ds, 0)).toBe(-45);
    plugin.afterLayout(fakeChart(cfg, { tickValues: [10] }));
    expect(alignAt(ds, 0)).toBe("top");
  });

  test("★ react-chartjs-2 重渲把 datalabels 换回原件后照样接得上", () => {
    const cfg = buildChartJSConfig(H1, STUB_THEME as any, { surface: "export" });
    const ds = cfg.data.datasets[0] as any;
    const plugin = tickAvoid(cfg);
    plugin.afterLayout(fakeChart(cfg));
    // `setDatasets` uses Object.assign for a same-name dataset: `_tickAvoid*` stays on the old object, while datalabels is replaced with the new original.
    Object.assign(ds, buildChartJSConfig(H1, STUB_THEME as any, { surface: "export" }).data.datasets[0]);
    expect(ds.datalabels.align).toBe("top");
    plugin.afterLayout(fakeChart(cfg));
    expect(alignAt(ds, 0)).toBe(-45);
  });

  test("右轴上冲突的末点朝左上摆", () => {
    // Keep months ascending (the date axis reorders chronologically), reversing only values so the maximum is the last point.
    const values = H1.rows!.map((r) => r.revenue).reverse();
    const rising: ChartSpec = { ...H1, rows: H1.rows!.map((r, i) => ({ ...r, revenue: values[i] })) };
    const cfg = buildChartJSConfig(rising, STUB_THEME as any, { surface: "export" });
    const ds = cfg.data.datasets[0] as any;
    tickAvoid(cfg).afterLayout(fakeChart(cfg, { tickRight: true }));
    expect(alignAt(ds, 5)).toBe(-135);
    expect(alignAt(ds, 0)).toBe("top");
  });

  test("插件挂进了 config", () => {
    expect(pluginIds(buildChartJSConfig(H1, STUB_THEME as any))).toContain("tick-label-avoid");
  });
});

// ── Do not thin category-axis labels (465 bar charts on 116 showed only 5 of 10 category labels) ─────────────
describe("类目轴刻度上限：只对时间轴生效", () => {
  const mk = (kind: string, xType: "categorical" | "date", n: number): ChartSpec => ({
    kind,
    title: "t",
    columns: [
      { key: "x", header: "x", type: xType },
      { key: "v", header: "v", type: "numeric" },
    ],
    rows: Array.from({ length: n }, (_, i) => ({ x: xType === "date" ? `2026年${i + 1}月` : `类目${i}`, v: i + 1 })),
  } as unknown as ChartSpec);
  // ⚠ Pass renderContext: without it, `recipe` is null and `maxXTicks` is already undefined,
  //    so the assertion passes **because it was absent already**, without testing this change.
  const xTicks = (spec: ChartSpec) =>
    ((buildChartJSConfig(spec, STUB_THEME as any, { renderContext: COMPACT_CONTEXT })
      .options as any).scales.x.ticks ?? {}).maxTicksLimit;

  test("类目 x 轴不设上限，交给 Chart.js 按真实标签宽度 autoSkip", () => {
    for (const kind of ["line", "bar"]) {
      expect(xTicks(mk(kind, "categorical", 20))).toBeUndefined();
    }
  });

  test("时间 x 轴保留上限（抽稀后仍能按相邻刻度插值读出来）", () => {
    for (const kind of ["line", "bar"]) {
      expect(xTicks(mk(kind, "date", 20))).toBe(4);
    }
  });

  test("scatter 的 x 是数值轴，不受这条影响", () => {
    expect(xTicks({
      kind: "scatter",
      title: "t",
      columns: [
        { key: "a", header: "a", type: "numeric" },
        { key: "b", header: "b", type: "numeric" },
      ],
      rows: [{ a: 1, b: 2 }, { a: 3, b: 4 }],
    } as unknown as ChartSpec)).toBe(4);
  });
});

// ── Value-axis titles (Chart Embed Spec): single-measure axes state only their unit, read from the unit field or
// the header's unit qualifier; counts and mixed units get no title ───────────────────────────────────────────────
describe("值轴标题：只写单位（单位在 unit 字段或列名限定语里）", () => {
  const yTitle = (spec: ChartSpec) =>
    ((buildChartJSConfig(spec, STUB_THEME as any).options as any).scales.y.title ?? {}).text;

  test("单序列：量纲只在列名里（116 实测 835 张）⇒ 从列名限定语读出单位，只写单位", () => {
    for (const kind of ["line", "bar"]) {
      expect(yTitle({
        kind, title: "t",
        columns: [
          { key: "省", header: "省", type: "geo" },
          { key: "营业额 / 元", header: "营业额 / 元", type: "numeric" },
        ],
        rows: [{ 省: "上海", "营业额 / 元": 1 }, { 省: "北京", "营业额 / 元": 2 }],
      } as unknown as ChartSpec)).toBe("元");
    }
  });

  test("计数列没有单位 ⇒ 不写轴标题（列名不是量纲，且与图题重复）", () => {
    expect(yTitle({
      kind: "bar", title: "领域覆盖",
      columns: [
        { key: "domain", header: "领域", type: "categorical" },
        { key: "entries", header: "管理条目", type: "numeric" },
      ],
      rows: [{ domain: "财务", entries: 12 }, { domain: "运营", entries: 4 }],
    } as unknown as ChartSpec)).toBeUndefined();
  });

  test("不带空格的斜杠是比值名、括号里的数字是限定语——都不当单位", () => {
    for (const header of ["销售额/面积", "营业额(2025)", "客流(近30天)"]) {
      expect(yTitle({
        kind: "bar", title: "t",
        columns: [
          { key: "店", header: "店", type: "categorical" },
          { key: "v", header, type: "numeric" },
        ],
        rows: [{ 店: "A", v: 1 }],
      } as unknown as ChartSpec), header).toBeUndefined();
    }
  });

  test("多列不同名共用一根轴：不标——拿第一列的名字当轴名就是把切片名挂成量纲", () => {
    expect(yTitle({
      kind: "bar", title: "各省分品类营业额分布",
      columns: [
        { key: "省", header: "省", type: "geo" },
        { key: "一次性内裤", header: "一次性内裤", type: "numeric" },
        { key: "一次性餐具", header: "一次性餐具", type: "numeric" },
      ],
      rows: [{ 省: "上海", 一次性内裤: 1, 一次性餐具: 2 }],
    } as unknown as ChartSpec)).toBeUndefined();
  });

  test("pivot 出来的系列列共用 measure_header：单位从度量名读，不从切片名读", () => {
    expect(yTitle({
      kind: "bar", title: "各省分品类",
      columns: [
        { key: "省", header: "省", type: "geo" },
        { key: "外套", header: "外套", type: "numeric", measure_header: "营业额 / 元" },
        { key: "上装", header: "上装", type: "numeric", measure_header: "营业额 / 元" },
      ],
      rows: [{ 省: "上海", 外套: 1, 上装: 2 }],
    } as unknown as ChartSpec)).toBe("元");
  });

  test("百分比列的轴标题是 %（既有行为，别回退）", () => {
    expect(yTitle({
      kind: "bar", title: "t",
      columns: [
        { key: "店", header: "店", type: "categorical" },
        { key: "退款率", header: "退款率", type: "numeric", format: "percent", percent_scale: "fraction" },
      ],
      rows: [{ 店: "A", 退款率: 0.12 }],
    } as unknown as ChartSpec)).toBe("%");
  });
});

// ── Percentages sharing an axis must share a scale (actual 116 dogfood screenshot) ─────────────────────
describe("同轴百分比统一档位", () => {
  /** Actual shape of 116's `8月营业额Top 3门店`: four percentage columns on the right axis, three points and one fraction. */
  const mixedSpec = (): ChartSpec => ({
    kind: "dual_axis", title: "8月营业额Top 3门店",
    columns: [
      { key: "门店", header: "门店", type: "categorical", y_axis_id: 0 },
      { key: "8月营业额", header: "8月营业额", type: "numeric", y_axis_id: 0 },
      // ⚠ Put the fraction column **first** on y1: the tick callback uses the scale tier of `lineCols[0]`;
      //   if a points column comes first, the callback already does no scaling and the assertion passes vacuously (even without this change).
      { key: "利润率", header: "利润率", type: "numeric", format: "percent", percent_scale: "fraction", y_axis_id: 1 },
      { key: "环比", header: "营业额环比变化率", type: "numeric", format: "percent", percent_scale: "points", y_axis_id: 1 },
    ],
    rows: [
      { 门店: "东湖街", "8月营业额": 128437519, 环比: 6.24, 利润率: 0.1987352816409153 },
      { 门店: "西岸二期", "8月营业额": 61273904, 环比: 2.09, 利润率: 0.172946103857214 },
    ],
  } as unknown as ChartSpec);

  const dsOf = (cfg: ReturnType<typeof buildChartJSConfig>, label: string) =>
    cfg.data.datasets.find((d: any) => d.label === label) as any;

  test("fraction 列换算到 points 再画——19.9% 不再被画在 0.199 的位置", () => {
    const cfg = buildChartJSConfig(mixedSpec(), STUB_THEME as any);
    expect(dsOf(cfg, "利润率").data[0]).toBeCloseTo(19.87352816409153, 6);
    expect(dsOf(cfg, "营业额环比变化率").data[0]).toBe(6.24);   // Keep points columns unchanged.
  });

  test("换算后该列的格式化档位改成 points，标签不会再乘一次", () => {
    const cfg = buildChartJSConfig(mixedSpec(), STUB_THEME as any);
    const ds = dsOf(cfg, "利润率");
    expect(ds._fmtCtx.percent).toBe("points");
    expect(ds.datalabels.formatter(ds.data[0])).toBe("19.87%");
  });

  test("混档那根轴的刻度回调不再二次缩放", () => {
    const cfg = buildChartJSConfig(mixedSpec(), STUB_THEME as any);
    expect((cfg.options as any).scales.y1.ticks.callback(20)).toBe("20");
  });

  test("mixed percentage hover reads the original observation after dataset cloning", () => {
    const cfg = buildChartJSConfig(mixedSpec(), STUB_THEME as any);
    const cloned = { ...cfg.data, datasets: cfg.data.datasets.map((ds) => ({ ...ds })) };
    const dataset = cloned.datasets.find((ds) => ds.label === "利润率")!;
    const callback = (cfg.options as any).plugins.tooltip.callbacks.label;
    expect(callback({ dataset, dataIndex: 0, parsed: { y: dataset.data[0] }, chart: { options: cfg.options } }))
      .toBe("利润率: 19.87%");
    expect(mixedSpec().rows![0].利润率).toBe(0.1987352816409153);
  });

  test("★负对照：轴上档位一致时一个字节都不动（fraction 轴仍由刻度回调 ×100）", () => {
    const spec = mixedSpec();
    // Change that points column to fraction too ⇒ no more mixed scale tiers. **Find it by key, not index**:
    // indices can target the wrong column when order changes (this negative control once failed falsely for that reason).
    (spec.columns as any).find((c: any) => c.key === "环比").percent_scale = "fraction";
    (spec.rows as any)[0].环比 = 0.0624;
    (spec.rows as any)[1].环比 = 0.0209;
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    expect(dsOf(cfg, "利润率").data[0]).toBeCloseTo(0.1987352816409153, 9);
    expect(dsOf(cfg, "利润率")._fmtCtx.percent).toBe("fraction");
    expect((cfg.options as any).scales.y1.ticks.callback(0.2)).toBe("20");
  });
});

// ── Scatter identity columns also recognize geo (previously every point in province scatter plots was unnamed) ───────────────────
describe("scatter 身份列认 geo", () => {
  const geoScatter = (n: number): ChartSpec => ({
    kind: "scatter", title: "各省营业额与客流量分布关系",
    columns: [
      { key: "省", header: "省", type: "geo" },
      { key: "营业额", header: "营业额_亿元", type: "numeric" },
      { key: "客流", header: "客流量 / 人次", type: "numeric" },
    ],
    rows: Array.from({ length: n }, (_, i) => ({ 省: `省${i}`, 营业额: i + 1, 客流: (i + 1) * 2 })),
  } as unknown as ChartSpec);

  test("身份数 ≤ 色板槽位：一省一条 dataset，图例能对上", () => {
    // STUB_THEME has a 4-color palette (production has 6), so use 3 provinces here.
    const cfg = buildChartJSConfig(geoScatter(3), STUB_THEME as any);
    expect(cfg.data.datasets).toHaveLength(3);
    expect(cfg.data.datasets.map((d: any) => d.label)).toEqual(["省0", "省1", "省2"]);
  });

  test("身份数 > 色板槽位：退回单系列 + 逐点标签（既有的 identityMode，别改）", () => {
    const cfg = buildChartJSConfig(geoScatter(30), STUB_THEME as any);
    expect(cfg.data.datasets).toHaveLength(1);
    expect((cfg.data.datasets[0] as any)._pointLabels).toHaveLength(30);
  });
});

// ── Give the line's 8% shaded area only to the series eligible for emphasis ─────────────────────────────
describe("折线阴影面积的归属", () => {
  const lineSpec = (n: number): ChartSpec => ({
    kind: "line", title: "t",
    columns: [
      { key: "月", header: "月", type: "date" },
      ...Array.from({ length: n }, (_, i) => ({ key: `v${i}`, header: `省${i}`, type: "numeric" })),
    ],
    rows: [1, 2, 3].map((m) => {
      const r: any = { 月: `2026年${m}月` };
      for (let i = 0; i < n; i++) r[`v${i}`] = m + i;
      return r;
    }),
  } as unknown as ChartSpec);
  const fills = (spec: ChartSpec) =>
    buildChartJSConfig(spec, STUB_THEME as any).data.datasets.map((d: any) => d.fill);

  test("单系列：照旧铺面积", () => {
    expect(fills(lineSpec(1))).toEqual(["origin"]);
  });

  test("多系列且没有主角：一条都不铺（第 0 条没有任何语义）", () => {
    expect(fills(lineSpec(4))).toEqual([false, false, false, false]);
  });

  test("多系列 + 图注点名：面积给主角，不给第 0 条", () => {
    const cfg = buildChartJSConfig(lineSpec(4), STUB_THEME as any, { captionText: "省2 全年领先" });
    const f = cfg.data.datasets.map((d: any) => d.fill);
    expect(f).toEqual([false, false, "origin", false]);
  });

  test("不变量：任何情况下最多只有一条线铺面积", () => {
    for (const n of [1, 2, 4, 8]) {
      for (const caption of [undefined, "省2 全年领先", "整体承压"]) {
        const f = buildChartJSConfig(lineSpec(n), STUB_THEME as any, { captionText: caption })
          .data.datasets.map((d: any) => d.fill);
        expect(f.filter((x: any) => x === "origin").length).toBeLessThanOrEqual(1);
      }
    }
  });
});

// ── Bar-family legends use color blocks, not point shapes (point shapes are not rendered on bars) ─────────────────────────────
describe("图例 marker：柱族用色块", () => {
  const many = (kind: string, n: number): ChartSpec => ({
    kind, title: "t",
    columns: [
      { key: "c", header: "品类", type: "categorical" },
      // ⚠ Do not use column names like `1月`…: they match "time-wide-table transposition" (timeHeaderPattern), turning 9 columns into 2 series.
      ...Array.from({ length: n }, (_, i) => ({ key: `m${i}`, header: `省${i}`, type: "numeric" })),
    ],
    rows: [1, 2].map((r) => {
      const o: any = { c: `类${r}` };
      for (let i = 0; i < n; i++) o[`m${i}`] = i + r;
      return o;
    }),
  } as unknown as ChartSpec);
  const usePoint = (spec: ChartSpec) =>
    (buildChartJSConfig(spec, STUB_THEME as any).options as any).plugins.legend.labels.usePointStyle;

  test("系列超过色板时，柱图图例仍用色块——点型在柱子上根本不渲染", () => {
    for (const kind of ["bar", "hbar", "stacked_bar"]) {
      expect(usePoint(many(kind, 9))).toBe(false);
    }
  });

  test("折线/面积保留点型——那些图上真的有点", () => {
    for (const kind of ["line", "area_stacked"]) {
      expect(usePoint(many(kind, 9))).toBe(true);
    }
  });

  test("系列没超色板时本来就不用点型（既有行为）", () => {
    expect(usePoint(many("bar", 2))).toBe(false);
    expect(usePoint(many("line", 2))).toBe(false);
  });
});

// ── Stacked pivot path: the axis has just one measure, so display its name ──────────────────────────
describe("堆叠 pivot 的轴名判据取真正落在轴上的列", () => {
  test("[分行, 渠道, 刷卡金额, 客户数] + pivot ⇒ 轴名是 sort_by 那个度量", () => {
    const cfg = buildChartJSConfig({
      kind: "stacked_bar", title: "各分行分渠道刷卡金额",
      sort_by: "刷卡金额",
      columns: [
        { key: "分行", header: "分行", type: "categorical" },
        { key: "渠道", header: "渠道", type: "categorical" },
        { key: "刷卡金额", header: "刷卡金额 / 元", type: "numeric" },
        { key: "客户数", header: "客户数", type: "numeric" },
      ],
      rows: [
        { 分行: "北京", 渠道: "线上", 刷卡金额: 10, 客户数: 1 },
        { 分行: "北京", 渠道: "线下", 刷卡金额: 20, 客户数: 2 },
        { 分行: "上海", 渠道: "线上", 刷卡金额: 30, 客户数: 3 },
      ],
    } as unknown as ChartSpec, STUB_THEME as any);
    // Only the plotted measure decides: the unrelated count column must not erase the unit.
    expect(((cfg.options as any).scales.y.title ?? {}).text).toBe("元");
  });
});

describe("exports follow the exporting user's hidden series", () => {
  const spec: ChartSpec = {
    kind: "line", title: "两线", row_count: 2,
    columns: [
      { key: "m", header: "月", type: "categorical" },
      { key: "a", header: "A", type: "numeric" },
      { key: "b", header: "B", type: "numeric" },
    ],
    rows: [{ m: "1", a: 1, b: 2 }, { m: "2", a: 3, b: 4 }],
  };
  const hidden = (cfg: ReturnType<typeof buildChartJSConfig>) =>
    (cfg.data.datasets as any[]).map((d) => d.hidden ?? false);

  test("an export hides what the exporting user hid, with no legend click handler", () => {
    const cfg = buildChartJSConfig(spec, STUB_THEME as any, { surface: "export", hiddenSeries: ["b"] });
    expect(hidden(cfg)).toEqual([false, true]);
    expect((cfg.options as any).plugins?.legend?.onClick).toBeUndefined();
  });

  test("an export without hidden series draws every series, as before", () => {
    expect(hidden(buildChartJSConfig(spec, STUB_THEME as any, { surface: "export", hiddenSeries: [] }))).toEqual([false, false]);
    expect(hidden(buildChartJSConfig(spec, STUB_THEME as any, { surface: "export" }))).toEqual([false, false]);
  });
});

describe("edge label padding stays on cartesian charts", () => {
  const donutSpec: ChartSpec = {
    kind: "pie", title: "Channel mix",
    columns: [
      { key: "ch", header: "Channel", type: "categorical" },
      { key: "v", header: "Revenue", type: "numeric" },
    ],
    rows: [{ ch: "Direct", v: 42 }, { ch: "Online", v: 26 }, { ch: "Partners", v: 18 }, { ch: "Enterprise", v: 9 }, { ch: "Other", v: 5 }],
    row_count: 5,
    show_data_labels: true,
  };

  test("a donut's first layout gets no side padding from the category-axis edge rule", () => {
    const cfg = buildChartJSConfig(donutSpec, STUB_THEME as any);
    const edgePad = (cfg.plugins ?? []).find((p) => p.id === "edge-label-padding") as any;
    expect(edgePad).toBeDefined();
    const ds = cfg.data.datasets[0] as any;
    const arg = { dataIndex: 0, datasetIndex: 0, dataset: ds };
    // Positive control: the first slice carries a label, so a category-axis rule would have measured one.
    const display = typeof ds.datalabels.display === "function" ? ds.datalabels.display(arg) : ds.datalabels.display;
    expect(display).toBeTruthy();
    let updates = 0;
    // First frame of a fresh donut: no x scale and no element coordinates, so the old code fell back to
    // chartArea.left/right as "first/last tick" and padded both sides by half a label. That shrank the bottom
    // legend's width and, depending on whether the chart was created fresh or updated, wrapped it differently.
    const chart = {
      options: {},
      data: cfg.data,
      scales: {},
      width: 310,
      chartArea: { top: 0, bottom: 200, left: 0, right: 310 },
      ctx: { save() {}, restore() {}, measureText: (s: string) => ({ width: s.length * 6 }), font: "" },
      isDatasetVisible: () => true,
      getDatasetMeta: () => ({ data: [] }),
      update: () => { updates += 1; },
    };
    edgePad.afterLayout(chart);
    expect((chart.options as any).layout?.padding).toBeUndefined();
    expect(updates).toBe(0);
  });
});


describe("point labels next to bars (116 dogfood: dual-axis -24.0% drawn on the bar at 390)", () => {
  const DUAL: ChartSpec = {
    kind: "dual_axis",
    title: "2026年各季度营业额及环比变化",
    row_count: 3,
    columns: [
      { key: "q", header: "季度", type: "categorical" },
      { key: "rev", header: "营业额", type: "numeric", unit: "亿元", y_axis_id: 0 },
      { key: "qoq", header: "环比变化率", type: "numeric", unit: "%", y_axis_id: 1, percent_scale: "points" },
    ],
    rows: [
      { q: "1季度", rev: 51.8, qoq: null },
      { q: "2季度", rev: 39.4, qoq: -24.0 },
      { q: "3季度", rev: 31.8, qoq: -19.2 },
    ],
  };
  /** Same line, short bars: every point sits above its bar. */
  const SHORT_BARS: ChartSpec = { ...DUAL, rows: DUAL.rows!.map((r) => ({ ...r, rev: 5 })) };
  const pluginOf = (cfg: ReturnType<typeof buildChartJSConfig>, id: string) =>
    (cfg.plugins ?? []).find((p) => p.id === id) as any;
  const placement = (cfg: ReturnType<typeof buildChartJSConfig>) => pluginOf(cfg, "point-label-placement");

  type FakeOpts = { barSize?: number; areaRight?: number; rightTicks?: number[]; padTop?: number };
  /** Plot x 30–300, y 10–200; bars at 75/165/255 (left axis 0–60), line on the right axis −30–0. */
  function fakeDualChart(cfg: ReturnType<typeof buildChartJSConfig>, opts: FakeOpts = {}) {
    const { barSize = 20, areaRight = 300, rightTicks = [], padTop } = opts;
    const datasets = cfg.data.datasets as any[];
    const y1Pixel = (v: number) => 10 + (-v / 30) * 190;
    const x = { axis: "x", getPixelForValue: (i: number) => 75 + i * 90 };
    const y = { axis: "y", getPixelForValue: (v: number) => 200 - (v / 60) * 190, getBasePixel: () => 200 };
    const y1 = {
      axis: "y",
      position: "right",
      options: { display: true, ticks: { display: true } },
      getPixelForValue: y1Pixel,
      getBasePixel: () => 10,
      // Tick text left-aligned 4px right of the plot, 12px line height (6px per character).
      getLabelItems: () =>
        rightTicks.map((v) => ({
          label: String(v),
          font: { string: "11px sans", lineHeight: 12 },
          textOffset: 0,
          options: { translation: [areaRight + 4, y1Pixel(v)], textAlign: "left", textBaseline: "middle" },
        })),
    };
    return {
      options: padTop === undefined ? {} : { layout: { padding: { top: padTop } } },
      data: cfg.data,
      scales: { x, y1 },
      chartArea: { left: 30, right: areaRight, top: 10, bottom: 200 },
      ctx: { save() {}, restore() {}, measureText: (s: string) => ({ width: s.length * 6 }), font: "" },
      isDatasetVisible: () => true,
      getDatasetMeta: (i: number) => {
        const ds = datasets[i];
        const getParsed = (j: number) => ({ x: j, y: ds.data[j] });
        return ds.type === "bar"
          ? {
              type: "bar",
              iScale: x,
              vScale: y,
              _stacked: false,
              controller: {
                getParsed,
                _getRuler: () => ({}),
                _calculateBarIndexPixels: (j: number) => ({ center: 75 + j * 90, size: barSize }),
              },
            }
          : { type: "line", iScale: x, vScale: y1, _stacked: false, controller: { getParsed } };
      },
    } as any;
  }
  const lineDs = (cfg: ReturnType<typeof buildChartJSConfig>) =>
    (cfg.data.datasets as any[]).find((ds) => ds.type === "line");
  const opt = (ds: any, key: string, dataIndex: number) => {
    const v = ds.datalabels[key];
    return typeof v === "function" ? v({ dataIndex, datasetIndex: 1, dataset: ds }) : v;
  };

  test("wide columns: line labels inside their bars step beside their own bar and stay", () => {
    const cfg = buildChartJSConfig(DUAL, STUB_THEME as any, { surface: "chat", forceDataLabels: true });
    const ds = lineDs(cfg);
    expect(opt(ds, "align", 1)).toBe("top"); // Before layout: the builder's default.
    placement(cfg).afterLayout(fakeDualChart(cfg));
    for (const i of [1, 2]) {
      expect(shown(opt(ds, "display", i))).toBe(true);
      expect(["left", "right"]).toContain(opt(ds, "align", i));
      expect(opt(ds, "offset", i)).toBe(13);
    }
  });

  test("packed columns: yielding labels hide rather than sit on or against another column (tooltip keeps the value)", () => {
    const cfg = buildChartJSConfig(DUAL, STUB_THEME as any, { surface: "chat", forceDataLabels: true });
    const ds = lineDs(cfg);
    expect(shown(opt(ds, "display", 1))).toBe(true); // Positive control: the builder labels both points.
    placement(cfg).afterLayout(fakeDualChart(cfg, { barSize: 60 }));
    expect(opt(ds, "display", 1)).toBe(false);
    expect(opt(ds, "display", 2)).toBe(false);
    // Bars keep their labels.
    const barDs = (cfg.data.datasets as any[]).find((d) => d.type === "bar");
    expect(shown(opt(barDs, "display", 1))).toBe(true);
  });

  test("an image capture (keep-all flag on the chart) keeps every label, moved off the bars", () => {
    const cfg = buildChartJSConfig(DUAL, STUB_THEME as any, { surface: "chat", forceDataLabels: true });
    const ds = lineDs(cfg);
    const chart = fakeDualChart(cfg);
    chart[POINT_LABEL_KEEP_ALL_KEY] = true;
    placement(cfg).afterLayout(chart);
    for (const i of [1, 2]) {
      expect(shown(opt(ds, "display", i))).toBe(true);
      expect(["left", "right"]).toContain(opt(ds, "align", i));
      expect(opt(ds, "offset", i)).toBe(13);
    }
  });

  test("surfaces without hover or an exact-value table keep every label (keepAll)", () => {
    const cfg = buildChartJSConfig(DUAL, STUB_THEME as any, {
      surface: "chat",
      forceDataLabels: true,
      renderContext: { ...COMPACT_CONTEXT, exactDataAlternative: { kind: "none" } },
    });
    const ds = lineDs(cfg);
    placement(cfg).afterLayout(fakeDualChart(cfg));
    expect(shown(opt(ds, "display", 1))).toBe(true);
    expect(shown(opt(ds, "display", 2))).toBe(true);
  });

  test("placement wins over tick avoidance: an endpoint next to the right-axis ticks moves below, not to an angle", () => {
    const cfg = buildChartJSConfig(SHORT_BARS, STUB_THEME as any, { surface: "chat", forceDataLabels: true });
    const ds = lineDs(cfg);
    // Q3 at x 255, y1(-19.2) ≈ 131.6: its "top" label box (≈ 234–276 × 113–128) reaches the tick text at x ≥ 270 (tick −17 at y ≈ 118).
    const chart = fakeDualChart(cfg, { areaRight: 266, rightTicks: [-17] });
    pluginOf(cfg, "tick-label-avoid").afterLayout(chart);
    expect(typeof ds._tickAvoidAlign?.get(2)).toBe("number"); // Positive control: tick avoidance shifted it.
    placement(cfg).afterLayout(chart);
    expect(opt(ds, "align", 2)).toBe("bottom");
    // Unchanged labels get an explicit entry too, so no tick-avoid angle can leak through.
    expect(ds._pointLabelPlacement.get(1)).toEqual({ align: "top", offset: 4 });
  });

  test("above the plot only the reserved padding is usable", () => {
    // A point at the very top of the right axis: its "top" label (y ≈ 1–15) would leave the plot (top 10).
    const top: ChartSpec = { ...SHORT_BARS, rows: SHORT_BARS.rows!.map((r, i) => (i === 2 ? { ...r, qoq: -1.5 } : r)) };
    const cfg = buildChartJSConfig(top, STUB_THEME as any, { surface: "chat", forceDataLabels: true });
    const ds = lineDs(cfg);
    placement(cfg).afterLayout(fakeDualChart(cfg));
    expect(opt(ds, "align", 2)).not.toBe("top");
    expect(shown(opt(ds, "display", 2))).toBe(true);
    placement(cfg).afterLayout(fakeDualChart(cfg, { padTop: 30 }));
    expect(opt(ds, "align", 2)).toBe("top");
  });

  test("layout changes re-run from the builder's options; the wrapper is installed once", () => {
    const cfg = buildChartJSConfig(SHORT_BARS, STUB_THEME as any, { surface: "chat", forceDataLabels: true });
    const ds = lineDs(cfg);
    const plugin = placement(cfg);
    plugin.afterLayout(fakeDualChart(cfg, { areaRight: 266, rightTicks: [-17] }));
    const wrapped = ds.datalabels;
    expect(opt(ds, "align", 2)).toBe("bottom");
    plugin.afterLayout(fakeDualChart(cfg));
    expect(ds.datalabels).toBe(wrapped);
    expect(opt(ds, "align", 2)).toBe("top");
  });

  test("the builders' marker never reaches into its own label: radius + slack fits inside the offset", () => {
    const ds = lineDs(buildChartJSConfig(DUAL, STUB_THEME as any, { surface: "chat", forceDataLabels: true }));
    // pointLabelPlacement skips a label's own marker; this pins why that is safe.
    expect(ds.pointRadius + 1).toBeLessThanOrEqual(ds.datalabels.offset);
  });

  test("bar geometry comes from Chart.js private BarController APIs that still exist", () => {
    const proto = BarController.prototype as unknown as Record<string, unknown>;
    expect(typeof proto._getRuler).toBe("function");
    expect(typeof proto._calculateBarIndexPixels).toBe("function");
  });

  test("line height follows the label font (Chart.js toLineHeight forms)", () => {
    expect(labelLineHeight(undefined, 12)).toBeCloseTo(14.4);
    expect(labelLineHeight(1.5, 12)).toBe(18);
    expect(labelLineHeight("20px", 12)).toBe(20);
    expect(labelLineHeight("150%", 12)).toBe(18);
  });

  test("exports keep their pinned pixels: the plugin is not attached there", () => {
    expect(pluginIds(buildChartJSConfig(DUAL, STUB_THEME as any, { surface: "export" }))).not.toContain(
      "point-label-placement",
    );
    expect(pluginIds(buildChartJSConfig(DUAL, STUB_THEME as any, { surface: "chat" }))).toContain(
      "point-label-placement",
    );
  });
});


describe("mixed native charts compare observed values in one tooltip", () => {
  test.each(["combo", "dual_axis"] as const)("%s preserves per-series units and percentage shape", (kind) => {
    const spec: ChartSpec = {
      kind, title: "Monthly performance", row_count: 2,
      columns: [
        { key: "month", header: "Month", type: "date" },
        { key: "revenue", header: "Revenue", type: "numeric", unit: "CNY 10k", y_axis_id: 0 },
        { key: "margin", header: "Margin", type: "numeric", format: "percent", percent_scale: "fraction", y_axis_id: 1 },
      ],
      rows: [{ month: "2026-01", revenue: 0, margin: null }, { month: "2026-02", revenue: 63.45678, margin: 0.4325 }],
    };
    const cfg = buildChartJSConfig(spec, STUB_THEME as any);
    expect(cfg.options?.interaction).toMatchObject({ mode: "index", axis: "x", intersect: false });
    const tooltip = (cfg.options as any).plugins.tooltip;
    expect(tooltip).toMatchObject({ mode: "index", axis: "x", intersect: false });
    const values = cfg.data.datasets.map((dataset) => tooltip.callbacks.label({ dataset: { ...dataset }, dataIndex: 1, parsed: { y: dataset.data[1] }, chart: { options: cfg.options } }));
    expect(values.join(" | ")).toContain("634,567.8");
    expect(values.join(" | ")).toContain("CNY");
    expect(values).toContain("Margin: 43.25%");
    expect(spec.rows![1].margin).toBe(0.4325);
  });
});

describe("population and encoded-domain formatting", () => {
  test("pie readouts and geometry use the visible nonnegative population", () => {
    const cfg = buildChartJSConfig({ kind: "pie", title: "Composition", row_count: 2, show_data_labels: true,
      columns: [{ key: "name", header: "Name", type: "categorical" }, { key: "v", header: "Value", type: "numeric" }],
      rows: [{ name: "A", v: 30 }, { name: "B", v: 70 }] }, STUB_THEME as any);
    const chart = { data: cfg.data, getDataVisibility: (i: number): boolean => i === 0 };
    const labels = (cfg.options.plugins!.legend!.labels as any).generateLabels(chart);
    expect(labels.map((item: any) => item.text)).toEqual(["A 100%", "B"]);
    expect((cfg.data.datasets[0] as any).datalabels.formatter(30, { chart })).toBe("100%");
    const tooltip = (cfg.options.plugins!.tooltip!.callbacks as any).label({ chart, label: "A", parsed: 30 });
    expect(tooltip).toContain("30"); expect(tooltip).toContain("Share of visible items: 100%");
    chart.getDataVisibility = () => false;
    expect((cfg.options.plugins!.tooltip!.callbacks as any).label({ chart, label: "A", parsed: 30 })).toContain("Share of visible items: —");
    expect((cfg.data.datasets[0] as any).datalabels.formatter(30, { chart })).toBe("");
  });
  test("waterfall scaling includes cumulative levels instead of contribution size", () => {
    const cfg = buildChartJSConfig({ kind: "waterfall", title: "Levels", row_count: 3,
      columns: [{ key: "step", header: "Step", type: "categorical" }, { key: "v", header: "Contribution", type: "numeric", unit: "USD" }],
      rows: [{ step: "Start", v: 398700 }, { step: "Change", v: -4500 }, { step: "End", v: 394200 }] }, STUB_THEME as any);
    const dataset = cfg.data.datasets[0] as any;
    expect(dataset.data[1]).toEqual([398700, 394200]);
    const tooltip = (cfg.options.plugins!.tooltip!.callbacks as any).label({ dataIndex: 1, raw: dataset.data[1] });
    expect(tooltip).toContain("4,500"); expect(tooltip).not.toContain("394,200");
  });
});

test("large declared percentages keep percentage-point ticks without an unnamed compact multiplier", () => {
  const cfg = buildChartJSConfig({ kind: "line", title: "Rates", row_count: 2,
    columns: [{ key: "x", header: "Period", type: "categorical" }, { key: "v", header: "Rate", type: "numeric", percent_scale: "fraction" }],
    rows: [{ x: "A", v: 0 }, { x: "B", v: 10000 }] }, STUB_THEME as any);
  const tick = (cfg.options.scales!.y!.ticks as any).callback;
  expect(tick(10000, 1, [{ value: 0 }, { value: 10000 }])).toBe("1,000,000");
});

test("horizontal date categories never replace the numeric value-axis callback", () => {
  const cfg = buildChartJSConfig({ kind: "hbar", title: "Monthly amounts", row_count: 2,
    columns: [{ key: "month", header: "Month", type: "date" }, { key: "v", header: "Value", type: "numeric" }],
    rows: [{ month: "2026-01", v: 0 }, { month: "2026-02", v: 50 }] }, STUB_THEME as any, { locale: "en-US" });
  expect((cfg.options.scales!.x!.ticks as any).callback(0)).toBe("0");
  expect((cfg.options.scales!.y!.ticks as any).callback(0)).toContain("Jan");
});

test("an unrelated date field does not rename categorical bars", () => {
  const cfg = buildChartJSConfig({ kind: "bar", title: "Regions", row_count: 2,
    columns: [{ key: "region", header: "Region", type: "categorical" }, { key: "month", header: "Month", type: "date" }, { key: "v", header: "Value", type: "numeric" }],
    rows: [{ region: "East", month: "2026-01", v: 10 }, { region: "West", month: "2026-02", v: 50 }] }, STUB_THEME as any);
  expect(cfg.data.labels).toEqual(["East", "West"]);
  expect(cfg.options.scales!.x!.ticks!.callback).toBeUndefined();
});
