import { describe, expect, it } from "vitest";
import { contextualEvidenceFixture, trendTableFixture } from "../../e2e/_chart-contextual-reading-fixture";
import { buildTrendTable, trendGeometry, type TableTrend } from "./table-trends";
import { evidencePlanForViewer } from "./presentation-plan";
import { exactTimeIndex } from "./time-window";
import { redactViewerSourceIdentitiesInValue } from "../canvas-pm-adapter/citation-types";
import { resolvePercentColumn } from "./format";

const labels = { start: "Start", end: "End", change: "Change", trend: "Trend", points: "pp" };
function table() {
  const fixture = trendTableFixture();
  return { ...fixture.spec.views![1].spec, presentation_plan: fixture.spec.presentation_plan, _export_table_trend: evidencePlanForViewer(fixture.spec.presentation_plan)!.table_trends![0] as TableTrend };
}
describe("contextual chart reading", () => {
  it("retains literal scope and rejects forged evidence relations or source bounds", () => {
    const fixture = contextualEvidenceFixture(), plan = evidencePlanForViewer(fixture.spec.presentation_plan)!;
    expect(plan).not.toBeNull();
    const forged = structuredClone(plan);
    const item = forged.annotations![0];
    if (item.availability === "available" && item.kind === "interval") item.upper = "2026-05";
    expect(evidencePlanForViewer(forged)).toBeNull();
    expect(evidencePlanForViewer({ ...plan, version: 1 })).toBeNull();
    const source = plan.evidence_notes[0].source_ref;
    const safe = redactViewerSourceIdentitiesInValue(fixture.spec, new Set([source.source_alias || source.id]));
    expect(JSON.stringify(safe)).not.toContain("Partial reporting period");
    expect(evidencePlanForViewer(safe.presentation_plan)!.annotations![0].availability).toBe("unavailable");
  });
  it("compares identical supplied endpoints, with nulls and absent observations intact", () => {
    const result = buildTrendTable(table(), labels)!;
    expect(result.spec.rows).toEqual([
      { __trend_entity: "A", __trend_start: null, __trend_end: 12.123456, __trend_change: null, __trend_series: '["A"]' },
      { __trend_entity: "B", __trend_start: -5, __trend_end: null, __trend_change: null, __trend_series: '["B"]' },
    ]);
    expect(result.domain).toEqual([-5, 12.123456]);
    const series = result.series.get('["A"]')!;
    expect(series.values).toEqual([null, 2, 12.123456]);
    expect(trendGeometry({ ...series, values: [1, null, 2] }, [0, 2]).path).not.toContain("L");
    expect(table().rows![0].revenue).toBe(12.123456);
  });
  it("formats fractional percentage differences as points without a second scaling", () => {
    const spec = table();
    spec.columns = spec.columns.map((column) => column.key === "revenue" ? { ...column, header: "Margin", format: "percent", percent_scale: "fraction", unit: "%" } : column);
    spec.rows = spec.rows!.map((row) => ({ ...row, revenue: row.month === "2026-01" ? 0.2 : 0.3 }));
    const result = buildTrendTable(spec, labels)!;
    expect(result.spec.rows![0].__trend_change).toBe(10);
    expect(resolvePercentColumn(result.spec.columns[3])).toBe(false);
    expect(result.spec.columns[3].unit).toBe("pp");
  });
  it("keeps original decimal endpoints and computes their native difference without a binary tail", () => {
    const spec = table();
    spec.rows = spec.rows!.map((row) => row.region === "A"
      ? { ...row, revenue: row.month === "2026-01" ? 52.12345 : row.month === "2026-03" ? 70.12345 : row.revenue }
      : row);
    const before = structuredClone(spec.rows);
    const result = buildTrendTable(spec, labels)!;
    expect(result.spec.rows![0]).toMatchObject({ __trend_start: 52.12345, __trend_end: 70.12345, __trend_change: 18 });
    expect(spec.rows).toEqual(before);
  });
  it("drops trend descriptors on dataset revocation and rejects duplicate original grain", () => {
    const spec = table();
    const safe = redactViewerSourceIdentitiesInValue(spec, new Set([evidencePlanForViewer(spec.presentation_plan)!.dataset.artifact_id]));
    expect(evidencePlanForViewer(safe.presentation_plan)!.table_trends).toEqual([{ view_id: "detail", availability: "unavailable" }]);
    spec.rows = [...spec.rows!, spec.rows![0]]; spec.row_count = spec.rows.length;
    expect(buildTrendTable(spec, labels)).toBeNull();
  });
  it("accepts exact prepared time keys without guessing, snapping or numeric coercion", () => {
    expect(exactTimeIndex(" 2026-03 ", ["2026-01", "2026-03"])).toBe(1);
    for (const text of ["2026-02", "2026/03", "March", "2026-02-30"]) expect(exactTimeIndex(text, ["2026-01", "2026-03"])).toBe(-1);
    expect(exactTimeIndex("2.026e3", [2026, 2027])).toBe(-1);
    expect(exactTimeIndex("2026", [2026, 2027])).toBe(0);
  });
});

it("reads source statements without exposing JSON syntax or inventing note text", async () => {
  const { sourceNoteStatement } = await import("./contextual-evidence");
  const plan = evidencePlanForViewer(contextualEvidenceFixture().spec.presentation_plan)!;
  const note = plan.evidence_notes[0];
  expect(sourceNoteStatement(note)).toMatchObject({ text: "Partial reporting period", period: "2026-01 – 2026-06" });
  expect(sourceNoteStatement({ ...note, availability: "unavailable" })).toBeNull();
  expect(sourceNoteStatement({ ...note, excerpt: "Ordinary source note" })).toBeNull();
  expect(sourceNoteStatement({ ...note, measure_keys: ["other"] })).toBeNull();
});
