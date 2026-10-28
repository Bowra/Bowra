import type { ReportSection } from "../lib/types";
import type { ChartSpec } from "../lib/chart/types";
import type { EvidencePlanV1 } from "../lib/chart/presentation-plan";

export const linkedFocusFixture = (): Extract<ReportSection, { kind: "chart" }> => {
  const columns: ChartSpec["columns"] = [
    { key: "region", header: "Region", type: "categorical" },
    { key: "revenue", header: "Revenue", type: "numeric", unit: "USD" },
    { key: "cost", header: "Cost", type: "numeric", unit: "USD" },
  ];
  const rows = [
    { region: "East", revenue: 52.12345, cost: 30.16789 },
    { region: "West", revenue: 30.12345, cost: 15.01234 },
    { region: "South", revenue: -5, cost: 12 },
    { region: "Missing", revenue: null, cost: 9 },
    { region: "North", revenue: 60, cost: 40 },
    { region: "Central", revenue: 70, cost: 45 },
    { region: "Coast", revenue: 40, cost: 25 },
    { region: "Other region", revenue: 80, cost: 55 },
  ];
  const base: ChartSpec = { kind: "scatter", title: "Revenue and cost", columns, rows, row_count: rows.length, data_artifact_id: "art_focus" };
  const bid = "entity.fixture";
  const plan: EvidencePlanV1 = {
    version: 1,
    dataset: { artifact_id: "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", content_digest: "a".repeat(64), schema_digest: "b".repeat(64), availability: "available" },
    evidence_notes: [],
    text_dependencies: Object.fromEntries(["/title", "/views/0/spec/title", "/views/1/spec/title", "/views/2/spec/title"].map((path) => [path, { dataset: true, note_ids: [] }])),
    view_bindings: { relationship: bid, comparison: bid, detail: bid },
    interactions: [
      { preset: "linked_focus", from_view: "relationship", to_views: ["comparison", "detail"], binding_id: bid },
      { preset: "linked_focus", from_view: "detail", to_views: ["relationship", "comparison"], binding_id: bid },
    ],
    bindings: { [bid]: { kind: "entity", availability: "available", dimension_keys: ["region"], observation_keys: ["region"], measure_keys: ["revenue", "cost"], eligible_views: ["relationship", "comparison", "detail"], coverage: { artifact_rows: { available: rows.length, total: rows.length }, views: { relationship: { retained_rows: rows.length }, comparison: { retained_rows: rows.length }, detail: { retained_rows: rows.length } } } } },
  };
  return { id: "sec_composition", kind: "chart", rev: 1, spec: { ...base, title: "Regional comparison", presentation_plan: plan,
    views: [
      { id: "relationship", kind: "chart", spec: base },
      { id: "comparison", kind: "chart", spec: { ...base, kind: "hbar", title: "Revenue by region", columns: columns.slice(0, 2), rows: [...rows].reverse() } },
      { id: "detail", kind: "chart", spec: { ...base, kind: "table", title: "Regional observations" } },
    ], layout: { type: "split", children: [{ view: "relationship" }, { type: "stack", children: [{ view: "comparison" }, { view: "detail" }] }] },
  } };
};
