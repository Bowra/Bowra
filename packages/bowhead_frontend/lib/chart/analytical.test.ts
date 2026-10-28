import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defaultChartTheme } from "../chart-theme";
import { evidencePlanForViewer } from "./presentation-plan";
import { analyticalBinding, analyticalX, analyticalDateTicks, groupedEntries, intervalPosition, validAnalyticalViews } from "./analytical";
import { buildChartJSConfig } from "./build-config";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import { applyChartEdit, validChartComposition } from "./composition";
import type { ChartSpec } from "./types";
import { seriesKeysOfDatasets } from "./series-visibility";
import { lastFiniteObservation } from "./line-endpoint-labels";

const fixtures: Record<string, { section: { spec: ChartSpec }; shape: unknown }> = JSON.parse(readFileSync(resolve(process.cwd(), "../../config/chart_style/analytical_fixtures.json"), "utf8"));

export function analyticalFixture(name: keyof typeof fixtures) {
  const root = structuredClone(fixtures[name].section.spec) as unknown as ChartSpec;
  const plan = evidencePlanForViewer(root.presentation_plan)!;
  return { root, plan, leaf: { ...root.views![0].spec, presentation_plan: plan, _export_analytical: plan.analytical_views![0] } as ChartSpec };
}

describe("native analytical figures", () => {
  it("aligns daily and monthly ticks to the original calendar granularity", () => {
    expect(analyticalDateTicks([Date.UTC(2026, 9, 10, 12), Date.UTC(2026, 9, 13, 12)], 10)).toEqual([Date.UTC(2026, 9, 11), Date.UTC(2026, 9, 12), Date.UTC(2026, 9, 13)]);
    expect(analyticalDateTicks([Date.UTC(2026, 0, 15), Date.UTC(2026, 3, 15)], 7)).toEqual([Date.UTC(2026, 1, 1), Date.UTC(2026, 2, 1), Date.UTC(2026, 3, 1)]);
  });
  for (const name of Object.keys(fixtures) as Array<keyof typeof fixtures>) it(`${name} preserves native identity, source binding and Python render shape`, () => {
    const { root, plan, leaf } = analyticalFixture(name);
    expect(plan).not.toBeNull();
    expect(validChartComposition(root)).toBe(true);
    expect(normalizeChartForRenderingV1(leaf)).toEqual({ spec: leaf, shape: fixtures[name].shape });
    expect(analyticalBinding(leaf)?.observation_keys).toEqual(leaf._export_analytical!.binding.observation_keys);
    expect(applyChartEdit(root, { scope: "view", viewId: "comparison" }, { show_legend: false }).views![0].spec.show_legend).toBe(false);
    expect(() => applyChartEdit(root, { scope: "view", viewId: "comparison" }, { kind: "bar" })).toThrow(/Analytical/);
  });
  it("keeps missing and equal bounds, outside points and sorted detail cells on their original rows", () => {
    const { leaf } = analyticalFixture("scenario_rows");
    const rows = [...leaf.rows!].sort((a, b) => String(b.city).localeCompare(String(a.city)));
    const entries = groupedEntries(rows, leaf._export_analytical);
    expect(entries.filter((entry) => !entry.row).map((entry) => entry.group)).toEqual(["Tier 1", "Tier 2", "Tier 3"]);
    expect(entries[1].row?.city).toBe("City 04");
    expect(entries.find((entry) => entry.row?.city === "City 03")?.row?.high).toBeNull();
    expect(entries.find((entry) => entry.row?.city === "City 07")?.row?.high).toBe(-46);
    expect(intervalPosition(-20, [-80, 0])).toBe(234);
  });
  it("plots actual unequal numeric gaps, null line breaks, exact row bindings and stable series identity", () => {
    const { leaf } = analyticalFixture("aligned_cycles");
    const config = buildChartJSConfig(leaf, defaultChartTheme(), { surface: "canvas", locale: "en-US", focusBinding: { descriptor: analyticalBinding(leaf)!, datasetDigest: "digest" } });
    expect(config.options.scales?.x?.type).toBe("linear");
    expect(config.data.datasets[0].data.slice(0, 5)).toEqual([{ x: -3, y: 74 }, { x: -1, y: 92 }, { x: 0, y: 100 }, { x: 1.25, y: 90 }, { x: 4, y: 68 }]);
    expect(config.data.datasets[2].data[4]).toEqual({ x: 4, y: null });
    expect(config.markBindings?.[0][3].observations[0].keyTuple).toEqual(["Primary", 1.25]);
    expect(seriesKeysOfDatasets(config.data.datasets)).toHaveLength(3);
    expect(lastFiniteObservation(config.data.datasets[0].data)).toBe(8);
    expect(analyticalX("2026-10", "date")).toBe(Date.UTC(2026, 9, 1));
  });
  it("rejects forged projected descriptors and accepts content-free unavailable stubs", () => {
    const { plan } = analyticalFixture("scenario_rows");
    expect(validAnalyticalViews([{ view_id: "comparison", availability: "unavailable" }])).toBe(true);
    expect(validAnalyticalViews([{ ...plan.analytical_views![0], pixels: 500 }])).toBe(false);
    expect(validAnalyticalViews([{ view_id: "comparison", availability: "unavailable", range: {} }])).toBe(false);
  });
});
