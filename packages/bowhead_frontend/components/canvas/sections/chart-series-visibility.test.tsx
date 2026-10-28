/**
 * The **two entries** for selecting a series (canvas legend / inspection chips) now write the same state.
 *
 * ★ The legend entry previously had **zero coverage**: legend.onClick was never overridden; Chart.js
 *   default handlers consumed clicks inside the instance, without a repository test enforcing it. This adds
 *   a previously absent gate rather than supplements an existing test.
 *
 * ⚠ Why mock buildChartJSConfig to return null: jsdom has no 2d context;
 *   returning actual config to ChartBlock makes ReactChart throw, then ErrorBoundary replaces
 *   all of ChartJsBlock with a fallback table, losing component state and preventing click/state-change tests.
 *   Returning null shows the fallback table **without unmounting**, preserving state.
 *   legendOnClickFromLastCall() constructs the actual handler using the **real implementation and the opts
 *   just passed by this component**, matching production code and callback.
 */

import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChartSpec } from "@/lib/chart/types";

const hoisted = vi.hoisted(() => ({
  calls: [] as Array<{ spec: ChartSpec; opts: Record<string, unknown> }>,
}));

vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    if (key === "rows_shown") return `rows ${values?.shown}/${values?.total}`;
    const labels: Record<string, string> = {
      open: "Inspect", close: "Close", tab_chart: "Chart", tab_data: "Data", view: "View",
      sort: "Sort", sort_none: "Default", top_n: "Top-N", top_n_all: "All",
      data_labels: "Labels", ref_line: "RefLine", rest_bucket: "其余",
      download_png: "PNG", download_csv: "CSV", followup: "AskAbout",
      more_actions: "More actions", open_data: "Open Data",
      "kind.bar": "Bar", "kind.hbar": "Horizontal Bar", "kind.line": "Line",
      "kind.area_stacked": "Stacked Area", "kind.stacked_bar": "Stacked Bar",
      "kind.dual_axis": "Dual Axis", "kind.combo": "Bar + Line", "kind.pie": "Pie",
      "kind.scatter": "Scatter", "kind.waterfall": "Waterfall", "kind.table": "Table",
    };
    return labels[key] ?? key;
  },
}));

vi.mock("@/lib/chart/build-config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/chart/build-config")>();
  const spy = vi.fn((spec: ChartSpec, _theme: unknown, opts: Record<string, unknown> = {}) => {
    hoisted.calls.push({ spec, opts });
    return null;
  });
  return { ...actual, buildChartJSConfig: spy as unknown as typeof actual.buildChartJSConfig };
});

import { ChartBlock } from "./chart-block";
import { ChartInspect } from "./chart-inspect";
import { exportHiddenSeriesFor, resetExportHiddenSeries } from "@/lib/chart/export-hidden-series";

const STUB_THEME = {
  ink: "#1A1916", inkSoft: "#57544D",
  primary: "#2C5F2D",
  palette: ["#2C5F2D", "#97BC62", "#F4A261", "#E76F51"],
  grid: "#E5E2DA",
  surface: "#FCFBF8",
};

/** One dimension + two numeric columns: legend items are series, and both entries are present. */
const twoSeriesSpec: ChartSpec = {
  kind: "dual_axis",
  title: "营业额与退款率",
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

type LegendOnClick = (e: unknown, item: { datasetIndex: number }, legend: unknown) => void;

function lastCall() {
  const call = hoisted.calls[hoisted.calls.length - 1];
  if (!call) throw new Error("buildChartJSConfig 没被调用——图块没渲到构图这一步");
  return call;
}

async function legendOnClickFromLastCall(): Promise<LegendOnClick> {
  const actual = await vi.importActual<typeof import("@/lib/chart/build-config")>(
    "@/lib/chart/build-config",
  );
  const call = lastCall();
  const cfg = actual.buildChartJSConfig(call.spec, STUB_THEME as never, call.opts as never);
  const onClick = (cfg.options as { plugins?: { legend?: { onClick?: LegendOnClick } } })
    .plugins?.legend?.onClick;
  if (!onClick) throw new Error("legend.onClick 没接上");
  return onClick;
}

const clickLegend = (onClick: LegendOnClick, datasetIndex: number) =>
  act(() => onClick({ type: "click" }, { datasetIndex }, {}));

/** Chip in the control row (fallback table may have a same-named column header; constrain scope). */
const seriesChip = (header: string) =>
  within(document.querySelector(".rc-inspect-ctrls") as HTMLElement)
    .getByRole("button", { name: header });

beforeEach(() => { hoisted.calls.length = 0; });
afterEach(() => cleanup());

describe("canvas 图例：点击写的是我们的状态", () => {
  it("★点第 2 个图例项 → hiddenSeries 里多出那一列的 key", async () => {
    render(<ChartBlock spec={twoSeriesSpec} />);
    expect(lastCall().opts.hiddenSeries).toEqual([]);

    clickLegend(await legendOnClickFromLastCall(), 1);

    expect(lastCall().opts.hiddenSeries).toEqual(["rate"]);
  });

  it("★再点一次 = 取消隐藏（语义仍是 toggle，不是 solo）", async () => {
    render(<ChartBlock spec={twoSeriesSpec} />);
    const onClick = await legendOnClickFromLastCall();

    clickLegend(onClick, 1);
    expect(lastCall().opts.hiddenSeries).toEqual(["rate"]);
    clickLegend(onClick, 1);
    expect(lastCall().opts.hiddenSeries).toEqual([]);
  });

  it("★点第二条不会把第一条放回来（本轮不做 solo）", async () => {
    render(<ChartBlock spec={twoSeriesSpec} />);
    const onClick = await legendOnClickFromLastCall();

    clickLegend(onClick, 1);
    clickLegend(onClick, 0);
    expect(lastCall().opts.hiddenSeries).toEqual(["rate", "amt"]);
  });

  it("★导出面不接这条交互（正对照：画布面接）", () => {
    render(<ChartBlock spec={twoSeriesSpec} surface="export" />);
    expect(lastCall().opts.onToggleSeries).toBeUndefined();
    expect(lastCall().opts.hiddenSeries).toEqual([]);
    cleanup();

    render(<ChartBlock spec={twoSeriesSpec} />);
    expect(lastCall().opts.onToggleSeries).toBeTypeOf("function");
    expect(lastCall().opts.hiddenSeries).toEqual([]);
  });
});

describe("检视层：图例与 chips 是同一份状态", () => {
  it("★点图例 → 控件排的 chip 跟着灭，数据页签也少一列", async () => {
    render(<ChartInspect spec={twoSeriesSpec} onClose={vi.fn()} />);
    expect(seriesChip("退款率")).toHaveAttribute("aria-pressed", "true");

    clickLegend(await legendOnClickFromLastCall(), 1);

    expect(seriesChip("退款率")).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    expect(screen.queryByRole("columnheader", { name: "退款率" })).toBeNull();
    expect(screen.getByRole("columnheader", { name: "营业额" })).toBeInTheDocument();
  });

  it("★反向：点 chip → 图那一侧收到的 hiddenSeries 也变了", () => {
    render(<ChartInspect spec={twoSeriesSpec} onClose={vi.fn()} />);

    fireEvent.click(seriesChip("退款率"));
    expect(lastCall().opts.hiddenSeries).toEqual(["rate"]);

    fireEvent.click(seriesChip("退款率")); // Click again to unhide
    expect(lastCall().opts.hiddenSeries).toEqual([]);
    expect(seriesChip("退款率")).toHaveAttribute("aria-pressed", "true");
  });

  it("★★关掉一条线不许偷换图种 —— 交给渲染器的 spec 一根列都没少", () => {
    // Before convergence, fallback notices guarded this: removing columns broke dual_axis shape requirements, triggering
    // canBuildKind → preferredKind fallback. Columns now stay intact, so fallback cannot occur.
    render(<ChartInspect spec={twoSeriesSpec} onClose={vi.fn()} />);
    fireEvent.click(seriesChip("退款率"));

    const { spec } = lastCall();
    expect(spec.kind).toBe("dual_axis");
    expect(spec.columns.map((c) => c.key)).toEqual(["m", "amt", "rate"]);
  });

  it("★图例那条入口同样不换图种（两个入口走的是同一条路）", async () => {
    render(<ChartInspect spec={twoSeriesSpec} onClose={vi.fn()} />);

    clickLegend(await legendOnClickFromLastCall(), 1);

    const { spec, opts } = lastCall();
    expect(spec.kind).toBe("dual_axis");
    expect(spec.columns).toHaveLength(3);
    expect(opts.hiddenSeries).toEqual(["rate"]);
  });
});

// Option (b), user decision 2026-10-01: an export follows the **exporting user's** legend-hidden series.
describe("exports follow the exporting user's hidden series", () => {
  beforeEach(() => resetExportHiddenSeries());

  it("a report chart records what its viewer hid, by section id, for their export request", async () => {
    render(<ChartBlock spec={twoSeriesSpec} sectionId="sec_a" />);
    expect(exportHiddenSeriesFor(["sec_a"])).toEqual({});
    clickLegend(await legendOnClickFromLastCall(), 1);
    expect(exportHiddenSeriesFor(["sec_a"])).toEqual({ sec_a: ["rate"] });
    clickLegend(await legendOnClickFromLastCall(), 1);
    expect(exportHiddenSeriesFor(["sec_a"])).toEqual({});
  });

  it("the record is cleared when the chart unmounts (e.g. switched to a table locally)", async () => {
    const { unmount } = render(<ChartBlock spec={twoSeriesSpec} sectionId="sec_a" />);
    clickLegend(await legendOnClickFromLastCall(), 1);
    expect(exportHiddenSeriesFor(["sec_a"])).toEqual({ sec_a: ["rate"] });
    unmount();
    expect(exportHiddenSeriesFor(["sec_a"])).toEqual({});
  });

  it("the inspection layer (controlled state) does not write the export record", async () => {
    render(<ChartInspect spec={twoSeriesSpec} onClose={vi.fn()} />);
    clickLegend(await legendOnClickFromLastCall(), 1);
    expect(exportHiddenSeriesFor(["sec_a", "undefined"])).toEqual({});
  });

  it("the export page's hidden series reach the chart build, without a click handler", () => {
    render(<ChartBlock spec={twoSeriesSpec} surface="export" hiddenSeries={["rate"]} />);
    expect(lastCall().opts.hiddenSeries).toEqual(["rate"]);
    expect(lastCall().opts.onToggleSeries).toBeUndefined();
  });
});

