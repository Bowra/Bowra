import { describe, expect, it } from "vitest";
import { chartThemeBaseForMode } from "../chart-theme";
import { sharedScaleFixture } from "../../e2e/_chart-shared-scale-fixture";
import { buildChartJSConfig } from "./build-config";
import { applyChartEdit } from "./composition";
import { evidencePlanForViewer } from "./presentation-plan";
import { sharedDomainsForView } from "./shared-scales";

const theme = chartThemeBaseForMode("light");

describe("native shared numeric axes", () => {
  it("keeps fixed bounds through local scope and legend changes without replacing values", () => {
    const root = sharedScaleFixture().spec;
    const groups = evidencePlanForViewer(root.presentation_plan)!.scale_groups!;
    const child = root.views![0].spec;
    const scoped = { ...child, rows: child.rows!.slice(3, 5), row_count: 2 };
    const domains = sharedDomainsForView(groups, "relationship");
    const config = buildChartJSConfig(scoped, theme, { axisDomains: domains, hiddenSeries: ["revenue"] });
    expect(config.options.scales!.y).toMatchObject({ min: -9.25, max: 84.25 });
    expect(config.data.datasets[0]).toMatchObject({ hidden: true });
    expect(scoped.rows!.map((row) => row.revenue)).toEqual([null, -5]);
    expect(child.rows).toHaveLength(6);
    const exported = buildChartJSConfig({ ...child, _export_axis_domains: domains }, theme, { surface: "export" });
    expect(exported.options.scales!.y).toMatchObject({ min: -9.25, max: 84.25 });
  });

  it("accepts display edits and rejects a shared encoding or percentage-stack change before PM writes", () => {
    const root = sharedScaleFixture().spec;
    const changed = applyChartEdit(root, { scope: "view", viewId: "relationship" }, { show_data_labels: true });
    expect(changed.views![1]).toBe(root.views![1]);
    expect(changed.presentation_plan).toBe(root.presentation_plan);
    expect(() => applyChartEdit(root, { scope: "view", viewId: "relationship" }, { kind: "hbar" })).toThrow(/shared numeric scale/);
    expect(() => applyChartEdit(root, { scope: "view", viewId: "relationship" }, { percent_stack: true })).toThrow(/shared numeric scale/);
  });

  it("does not mistake unavailable dataset seals for new domains or accept forged projected fields", () => {
    const root = sharedScaleFixture().spec;
    const plan = structuredClone(root.presentation_plan);
    const original = evidencePlanForViewer(plan)!;
    original.dataset = { artifact_id: original.dataset.artifact_id, availability: "unavailable" };
    original.bindings = { "time.fixture": { kind: "time", availability: "unavailable" } };
    expect(evidencePlanForViewer(plan)?.scale_groups).toEqual(evidencePlanForViewer(root.presentation_plan)!.scale_groups);
    original.scale_groups![0].domain = [Infinity, 1];
    expect(evidencePlanForViewer(plan)).toBeNull();
  });

  it("applies scatter x and dual-axis y1 bounds independently, keeping fraction observations raw", () => {
    const child = sharedScaleFixture().spec.views![0].spec;
    const scatter = buildChartJSConfig({ ...child, kind: "scatter" }, theme, { axisDomains: { encoding: "scatter", domains: { x: [-10, 90] } } });
    expect(scatter.options.scales!.x).toMatchObject({ min: -10, max: 90 });
    expect(scatter.options.scales!.y?.min).toBeUndefined();
    const columns = child.columns.map((column) => column.key === "cost" ? { ...column, unit: "%", format: "percent" as const, percent_scale: "fraction" as const, y_axis_id: 1 as const } : column);
    const dual = buildChartJSConfig({ ...child, kind: "dual_axis", columns, rows: child.rows!.map((row) => ({ ...row, cost: Number(row.cost) / 100 })) }, theme, { axisDomains: { encoding: "dual_axis", domains: { y1: [0, 0.6] } } });
    expect(dual.options.scales!.y1).toMatchObject({ min: 0, max: 0.6 });
    expect(dual.data.datasets[1].data).toContain(0.55);
    expect(dual.options.scales!.y?.max).toBeUndefined();
  });
});
