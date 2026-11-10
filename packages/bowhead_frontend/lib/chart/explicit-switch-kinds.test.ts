import { describe, expect, test } from "vitest";

import fixtures from "../../../../config/chart_style/explicit_switch_kinds_fixtures.json";
import { applyChartEdit } from "./composition";
import { explicitSwitchKinds } from "./explicit-switch-kinds";
import type { ChartSpec } from "./types";

type Fixture = (typeof fixtures.fixtures)[number] & { generatedRows?: { count: number; x: string; y: string } };

function specOf(fixture: Fixture): ChartSpec {
  const spec = structuredClone(fixture.spec) as unknown as ChartSpec;
  const generated = fixture.generatedRows;
  if (!generated) return spec;
  const rows = Array.from({ length: generated.count }, (_, index) => ({ [generated.x]: `b${index}`, [generated.y]: index }));
  return { ...spec, rows, row_count: rows.length };
}

const cases = fixtures.fixtures as Fixture[];

describe("declared-axis chart type switching", () => {
  // `test_chat_and_report_switcher_offer_the_same_declared_axis_kinds` runs the same fixture against `edit_chart`.
  test.each(cases.map((fixture) => [fixture.name, fixture] as const))("%s", (_name, fixture) => {
    expect(explicitSwitchKinds(specOf(fixture))).toEqual(fixture.expected);
  });

  test("a saved switch keeps the declared binding, rows and source", () => {
    const bar = specOf(cases.find((fixture) => fixture.name.startsWith("ungrouped bar"))!);
    const area = applyChartEdit(bar, { scope: "root" }, { kind: "area_stacked" });
    expect(area).toEqual({ ...bar, kind: "area_stacked" });
  });

  test("a saved edit refuses a type outside the offered set", () => {
    const bar = specOf(cases.find((fixture) => fixture.name.startsWith("ungrouped bar"))!);
    expect(() => applyChartEdit(bar, { scope: "root" }, { kind: "pie" })).toThrow();
    const single = specOf(cases.find((fixture) => fixture.name.startsWith("a single row"))!);
    expect(() => applyChartEdit(single, { scope: "root" }, { kind: "line" })).toThrow();
  });

  test("a composed view keeps its declared type, as chat edits change only the root", () => {
    const bar = specOf(cases.find((fixture) => fixture.name.startsWith("ungrouped bar"))!);
    const composed: ChartSpec = {
      ...bar, views: [{ id: "a", kind: "chart", spec: bar }, { id: "b", kind: "chart", spec: bar }],
      layout: { type: "stack", children: [{ view: "a" }, { view: "b" }] },
    };
    expect(() => applyChartEdit(composed, { scope: "view", viewId: "a" }, { kind: "line" })).toThrow();
    expect(applyChartEdit(composed, { scope: "view", viewId: "a" }, { title: "Renamed" }).views![0]!.spec.title).toBe("Renamed");
  });
});
