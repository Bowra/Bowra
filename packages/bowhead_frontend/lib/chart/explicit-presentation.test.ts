// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { resolveChartTheme } from "@/lib/chart-theme";
import { buildChartJSConfig } from "./build-config";
import { fitHbarRowsForExport } from "./export-fit";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import { pivotLongSpec } from "./pivot-long";
import { ensureFullRows } from "./view-transforms";
import { fetchFilePreview } from "../api";
import type { ChartSpec } from "./types";

vi.mock("../api", () => ({ fetchFilePreview: vi.fn() }));

function plot(): ChartSpec {
  return {
    kind: "bar", title: "Hospitals", row_count: 3,
    columns: [
      { key: "decile", header: "Decile", type: "categorical", role: "x" },
      { key: "region", header: "Region", type: "categorical", role: "color" },
      { key: "count", header: "Hospitals", type: "numeric", role: "y" },
    ],
    rows: [{ decile: "9", region: "South", count: 14 }, { decile: "10", region: "South", count: 12 }, { decile: "2", region: "South", count: 1 }],
    explicit_presentation: { version: 1, mode: "plot", axes: { x: "decile", y: ["count"], x_scale: "category" }, group_by: ["region"] },
  };
}

describe("saved explicit presentation", () => {
  it("renders the exact Decile axis and constant declared group", () => {
    const spec = plot(), before = JSON.stringify(spec);
    const config = buildChartJSConfig(spec, resolveChartTheme());
    expect(config.data.labels).toEqual(["9", "10", "2"]);
    expect(config.data.datasets.map((series) => series.label)).toEqual(["South"]);
    expect(config.data.datasets[0]!.data).toEqual([14, 12, 1]);
    expect(JSON.stringify(spec)).toBe(before);
  });

  it("uses explicit sparse grouping without a coverage heuristic", () => {
    const spec = plot();
    spec.rows![1]!.region = "East";
    spec.rows![2]!.region = "West";
    const pivot = pivotLongSpec(spec)!;
    expect(pivot.spec.rows).toHaveLength(3);
    expect(pivot.spec.rows![0]!.__pivot__East).toBeNull();
    expect(pivot.seriesCount).toBe(3);
  });

  it("rejects repeated cells instead of silently summing observations", () => {
    const spec = plot();
    spec.rows!.push({ ...spec.rows![0]!, count: 2 });
    expect(() => pivotLongSpec(spec)).toThrow(/unique/);
  });

  it("does not infer a split when the saved group list is empty", () => {
    const spec = plot();
    spec.explicit_presentation!.group_by = [];
    expect(pivotLongSpec(spec)).toBeNull();
  });

  it("preserves explicit date order and does not transpose time-named value columns", () => {
    const spec: ChartSpec = {
      kind: "line", title: "Prepared", row_count: 2,
      columns: [{ key: "date", header: "Date", type: "date" }, ...["2026-01", "2026-02", "2026-03"].map((key) => ({ key, header: key, type: "numeric" as const }))],
      rows: [{ date: "2026-02-01", "2026-01": 1, "2026-02": 2, "2026-03": 3 }, { date: "2026-01-01", "2026-01": 4, "2026-02": 5, "2026-03": 6 }],
      explicit_presentation: { version: 1, mode: "plot", axes: { x: "date", y: ["2026-01", "2026-02", "2026-03"], x_scale: "category" }, group_by: [] },
    };
    expect(normalizeChartForRenderingV1(spec).spec.rows).toEqual(spec.rows);
  });

  it("exports every explicit horizontal bar instead of selecting by magnitude", () => {
    const spec = plot();
    spec.kind = "hbar";
    spec.rows = Array.from({ length: 45 }, (_, index) => ({ decile: String(index), region: "South", count: index }));
    spec.row_count = 45;
    expect(fitHbarRowsForExport(spec, 400)).toBe(spec);
    expect(buildChartJSConfig(spec, resolveChartTheme()).data.labels).toHaveLength(45);
  });
});


it("expands an exact Table by source keys even when translated headers collide", async () => {
  const spec: ChartSpec = { kind: "table", title: "Selected", data_artifact_id: "art_table", row_count: 2,
    columns: [{ key: "code", header: "count", type: "categorical" }, { key: "count", header: "Total", type: "numeric" }],
    rows: [{ code: "001", count: 1 }], explicit_presentation: { version: 1, mode: "table", columns: ["code", "count"] } };
  vi.mocked(fetchFilePreview).mockResolvedValue({ family: "table", mode: "rows", columns: ["extra", "code", "count"], text: null,
    rows: [{ extra: "hidden", code: "001", count: "1" }, { extra: "hidden", code: "002", count: "2" }] } as never);
  const result = await ensureFullRows(spec);
  expect(result.ok).toBe(true);
  expect(result.spec.rows).toEqual([{ code: "001", count: 1 }, { code: "002", count: 2 }]);
  expect(result.spec.columns).toEqual(spec.columns);
  expect(result.spec.explicit_presentation).toEqual(spec.explicit_presentation);
});

it("keeps saved grouping stable when composed/export consumers normalize twice", () => {
  const first = normalizeChartForRenderingV1(plot()).spec;
  const second = normalizeChartForRenderingV1(first).spec;
  expect(second).toEqual(first);
  expect(second.explicit_presentation!.group_by).toEqual(["region"]);
  expect(buildChartJSConfig(second, resolveChartTheme()).data.datasets[0]!.data).toEqual([14, 12, 1]);
});


it("keeps observation context out of plotted series across repeated normalization", () => {
  const spec = plot();
  spec.columns.push({ key: "frame", header: "Month", type: "date" }, { key: "id", header: "Record ID", type: "numeric" });
  spec.rows = spec.rows!.map((row, index) => ({ ...row, frame: "2026-02", id: index + 100 }));
  const first = normalizeChartForRenderingV1(spec).spec;
  const second = normalizeChartForRenderingV1(first).spec;
  expect(second).toEqual(first);
  expect(first.columns.map((column) => column.key)).toEqual(["decile", "__pivot__South"]);
  expect(buildChartJSConfig(second, resolveChartTheme()).data.datasets.map((series) => series.label)).toEqual(["South"]);
  expect(spec.rows[0]!.id).toBe(100);
});

it("honors category x even when the declared grouping field is a date", () => {
  const spec = plot();
  spec.kind = "line";
  spec.columns[1]!.type = "date";
  spec.rows!.forEach((row) => { row.region = "2026-02"; });
  const config = buildChartJSConfig(spec, resolveChartTheme());
  expect(config.data.labels).toEqual(["9", "10", "2"]);
  expect(config.data.datasets.map((series) => series.label)).toEqual(["2026-02"]);
  expect(config.data.datasets[0]!.data).toEqual([14, 12, 1]);
});


it.each(["categorical", "date"] as const)("keeps every explicitly declared scatter group of type %s", (type) => {
  const spec: ChartSpec = { kind: "scatter", title: "Declared groups", row_count: 9,
    columns: [{ key: "x", header: "X", type: "numeric" }, { key: "y", header: "Y", type: "numeric" }, { key: "group", header: "Group", type }],
    rows: Array.from({ length: 9 }, (_, index) => ({ x: index, y: index + 10, group: type === "date" ? `2026-0${index + 1}` : `Group ${index}` })),
    explicit_presentation: { version: 1, mode: "plot", axes: { x: "x", y: ["y"], x_scale: "linear" }, group_by: ["group"] } };
  const normalized = normalizeChartForRenderingV1(spec);
  const config = buildChartJSConfig(spec, resolveChartTheme());
  expect(normalized.shape.seriesCount).toBe(9);
  expect(config.data.datasets).toHaveLength(9);
  expect(config.data.datasets.map((dataset) => dataset.label)).toEqual(spec.rows!.map((row) => row.group));
});


it.each([true, false])("honors explicit legend visibility %s for a constant declared group on a real render surface", (visible) => {
  const spec = plot();
  spec.show_legend = visible;
  const config = buildChartJSConfig(spec, resolveChartTheme(), { renderContext: {
    placement: "canvas", bounds: { widthPx: 900, heightPx: 400, devicePixelRatio: 1 },
    input: { hover: true, pointer: "fine", keyboard: true }, output: { medium: "screen", static: false },
    mode: "light", locale: "en-US", reducedMotion: false, forcedColors: false,
    exactDataAlternative: { kind: "inspect-table" },
  } });
  expect(config.data.datasets.map((series) => series.label)).toEqual(["South"]);
  expect(config.options.plugins?.legend?.display).toBe(visible);
});
